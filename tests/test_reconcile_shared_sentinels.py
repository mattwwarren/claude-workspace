"""Unit tests for cw.reconcile._shared — transcript/sentinel state detection.

Roster/drift detection, transcript liveness + staleness, locate-session,
backfill, sentinel parsing, salvage-terminal status classification, and
_detect_post_review_clean (R6).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import freezegun
import pytest

from cw._util import claude_project_dir
from cw.auto_dev_result import (
    AutoDevResult,
    BlockedResult,
    parse_stdout,
)
from cw.config import (
    load_state,
    save_state,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    _STAGE_REVIEW_COMPLETE,
    SPAWN_GRACE_SECONDS,
    _claude_agents_json,
    _has_terminal_sentinel,
    compute_drift,
    reconcile,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from tests._reconcile_helpers import (
    SCOPE_GUARD_FILES,
    SCOPE_GUARD_LINES,
    _inflate_scope,
    _make_stale_base_repo,
    _make_terminal_payload,
    _mk_daemon_session_with_worktree,
    _mk_headless_daemon_session,
    _mk_session,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _ul_record,
    _write_idle_transcript_with_text,
    _write_salvage_transcript,
    _write_transcript_records,
)
from tests.conftest import (
    _make_daemon_session,
    _write_idle_transcript,
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

    detection = _detect_usage_limit(sess)
    assert detection.detected is True
    # No top-level "timestamp" on the record → no anchors available.
    assert detection.matched_at is None
    assert detection.transcript_tail_at is None


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

    assert _detect_usage_limit(sess).detected is False


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

    detection = _detect_usage_limit(sess)
    assert detection.detected is False
    assert detection.matched_at is None
    assert detection.transcript_tail_at is None


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

    detection = _detect_usage_limit(sess)
    assert detection.detected is False
    assert detection.matched_at is None
    assert detection.transcript_tail_at is None


# ---------------------------------------------------------------------------
# #1345: usage-limit recency bound — matched_at / transcript_tail_at tracking
# ---------------------------------------------------------------------------

_UL_TEXT = "You've hit your session limit · resets 5:20pm"


def _stamp_after_start(transcript: Path, started_at: datetime) -> None:
    """Stamp mtime after started_at so the stale-transcript guard doesn't fire."""
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))


def test_detect_usage_limit_matched_at_is_last_matching_record_timestamp(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """matched_at is the LAST matching record's timestamp (last-match-wins)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-lastmatch"
    sess = _mk_headless_daemon_session("lastmatch", worktree, started_at)

    t0 = "2026-01-01T00:00:10+00:00"
    t1 = "2026-01-01T00:00:20+00:00"
    t2 = "2026-01-01T00:00:30+00:00"
    t3 = "2026-01-01T00:00:40+00:00"
    transcript = _write_transcript_records(
        home,
        worktree,
        [
            _ul_record(_UL_TEXT, t0),
            _ul_record("continuing work", t1),
            _ul_record("You've hit your usage limit again", t2),
            _ul_record("all done", t3),
        ],
    )
    _stamp_after_start(transcript, started_at)

    detection = _detect_usage_limit(sess)
    assert detection.detected is True
    assert detection.matched_at == datetime.fromisoformat(t2)
    assert detection.transcript_tail_at == datetime.fromisoformat(t3)


def test_detect_usage_limit_transcript_tail_at_tracks_last_record_even_when_unmatched(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transcript_tail_at follows the last content record even if it's unmatched."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-tailunmatched"
    sess = _mk_headless_daemon_session("tailunmatched", worktree, started_at)

    t0 = "2026-01-01T00:00:10+00:00"
    t1 = "2026-01-01T00:10:00+00:00"
    transcript = _write_transcript_records(
        home,
        worktree,
        [
            _ul_record(_UL_TEXT, t0),
            _ul_record("later unrelated work", t1),
        ],
    )
    _stamp_after_start(transcript, started_at)

    detection = _detect_usage_limit(sess)
    assert detection.detected is True
    assert detection.matched_at == datetime.fromisoformat(t0)
    assert detection.transcript_tail_at == datetime.fromisoformat(t1)


def test_detect_usage_limit_matched_at_none_when_matching_record_has_no_timestamp(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching record without a parseable timestamp yields matched_at None."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-matchnots"
    sess = _mk_headless_daemon_session("matchnots", worktree, started_at)

    t1 = "2026-01-01T00:00:20+00:00"
    transcript = _write_transcript_records(
        home,
        worktree,
        [
            _ul_record(_UL_TEXT, None),
            _ul_record("done", t1),
        ],
    )
    _stamp_after_start(transcript, started_at)

    detection = _detect_usage_limit(sess)
    assert detection.detected is True
    assert detection.matched_at is None
    # The later non-matching record still carries a parseable tail timestamp.
    assert detection.transcript_tail_at == datetime.fromisoformat(t1)


def test_detect_usage_limit_tail_none_when_no_record_has_timestamp(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No record carries a parseable timestamp → transcript_tail_at is None."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-nots"
    sess = _mk_headless_daemon_session("nots", worktree, started_at)

    transcript = _write_transcript_records(
        home,
        worktree,
        [
            _ul_record(_UL_TEXT, None),
            _ul_record("done", None),
        ],
    )
    _stamp_after_start(transcript, started_at)

    detection = _detect_usage_limit(sess)
    assert detection.detected is True
    assert detection.matched_at is None
    assert detection.transcript_tail_at is None


# ---------------------------------------------------------------------------
# #1345: usage-limit recency bound — _usage_limit_is_recent contract
# ---------------------------------------------------------------------------


def test_usage_limit_is_recent_false_when_not_detected() -> None:
    """Not detected → always False, regardless of window."""
    from cw.reconcile._shared import UsageLimitDetection, _usage_limit_is_recent

    detection = UsageLimitDetection(
        detected=False, matched_at=None, transcript_tail_at=None
    )
    assert _usage_limit_is_recent(detection, window_seconds=300) is False


def test_usage_limit_is_recent_true_when_match_at_tail() -> None:
    """Match landing at the transcript tail (gap 0) is recent."""
    from cw.reconcile._shared import UsageLimitDetection, _usage_limit_is_recent

    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    detection = UsageLimitDetection(detected=True, matched_at=ts, transcript_tail_at=ts)
    assert _usage_limit_is_recent(detection, window_seconds=300) is True


def test_usage_limit_is_recent_false_when_gap_exceeds_window() -> None:
    """A limit message far before the tail is stale (early+unrelated scenario)."""
    from cw.reconcile._shared import UsageLimitDetection, _usage_limit_is_recent

    matched = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    tail = matched + timedelta(seconds=301)
    detection = UsageLimitDetection(
        detected=True, matched_at=matched, transcript_tail_at=tail
    )
    assert _usage_limit_is_recent(detection, window_seconds=300) is False


def test_usage_limit_is_recent_boundary_gap_equals_window_is_recent() -> None:
    """Gap exactly equal to the window is recent (<= boundary)."""
    from cw.reconcile._shared import UsageLimitDetection, _usage_limit_is_recent

    matched = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    tail = matched + timedelta(seconds=300)
    detection = UsageLimitDetection(
        detected=True, matched_at=matched, transcript_tail_at=tail
    )
    assert _usage_limit_is_recent(detection, window_seconds=300) is True


def test_usage_limit_is_recent_fails_open_when_timestamps_missing_default() -> None:
    """Detected but no anchor → returns fail_open (default True)."""
    from cw.reconcile._shared import UsageLimitDetection, _usage_limit_is_recent

    detection = UsageLimitDetection(
        detected=True, matched_at=None, transcript_tail_at=None
    )
    assert _usage_limit_is_recent(detection, window_seconds=300) is True


def test_usage_limit_is_recent_fails_closed_when_missing_and_no_fail_open() -> None:
    """Detected but no anchor with fail_open=False → returns False."""
    from cw.reconcile._shared import UsageLimitDetection, _usage_limit_is_recent

    detection = UsageLimitDetection(
        detected=True, matched_at=None, transcript_tail_at=datetime.now(UTC)
    )
    assert (
        _usage_limit_is_recent(detection, window_seconds=60, fail_open=False) is False
    )


def test_iter_assistant_records_skips_malformed_and_parses_valid(
    tmp_path: Path,
) -> None:
    """_iter_assistant_records skips every non-conforming record shape and a
    malformed timestamp, yielding only well-formed assistant records."""
    from cw.reconcile._shared import _iter_assistant_records

    path = tmp_path / "mixed.jsonl"
    lines = [
        "{not valid json",  # JSONDecodeError → skip
        json.dumps([1, 2, 3]),  # non-dict record → skip
        json.dumps({"type": "user", "message": {"role": "user"}}),  # not assistant
        json.dumps({"type": "assistant", "message": "oops"}),  # message not dict
        json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": "x"}}
        ),  # content not list → skip
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "not-a-timestamp",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "bad ts"}],
                },
            }
        ),  # malformed timestamp → ts None, still yielded
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:05+00:00",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "good"}],
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")

    results = list(_iter_assistant_records(path))

    assert results == [
        (None, "bad ts"),
        (datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC), "good"),
    ]


def test_iter_assistant_records_returns_empty_on_missing_file(
    tmp_path: Path,
) -> None:
    """A missing transcript file (OSError on open) yields nothing."""
    from cw.reconcile._shared import _iter_assistant_records

    assert list(_iter_assistant_records(tmp_path / "does-not-exist.jsonl")) == []


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
    salvaged and routed to BLOCKED_ON_USER, NOT reverted to PENDING and NOT
    silently marked COMPLETED (#431, #1566).

    Covers the bug where _SALVAGE_TERMINAL_STATUSES was {"shipped", "no_op"} and
    missed scope_exceeded, forbidden_area, plan_pending_approval,
    review_pending_approval, merge_gate_blocked, and the PAUSED_FOR_USER_INPUT
    statuses. scope_exceeded/forbidden_area/merge_gate_blocked additionally must
    NOT be marked COMPLETED (#1566): live dispatch routes them to
    BLOCKED_ON_USER, so salvage must agree.
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
    assert task.status == QueueItemStatus.BLOCKED_ON_USER, (
        f"status={status!r}: queue task must be BLOCKED_ON_USER, not "
        "silently marked COMPLETED (#1566)"
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


def test_salvage_merge_pending_from_phantom_routes_to_blocked_on_user(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom session whose transcript emits merge_pending must be salvaged
    to BLOCKED_ON_USER, matching dispatch Rule 3b (#899, #1566). No existing
    coverage exercised this status through the salvage path before #1566."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    status = "merge_pending"
    ticket_id = "899p-merge_pending"
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

    alive = _mk_session("alive-899", surface_ref="live-ref")
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
        "merge_pending ticket must NOT be reverted to PENDING (salvaged terminal)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        "session must be COMPLETED after salvage"
    )
    assert reloaded.completed_reason == CompletionReason.NORMAL
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.BLOCKED_ON_USER, (
        "merge_pending queue task must be BLOCKED_ON_USER, matching dispatch Rule 3b"
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


def test_salvage_dispatch_hold_membership_is_single_source_of_truth() -> None:
    """Drift guard (#1566): queue_status_for_terminal_sentinel's hold set must
    agree with live dispatch's Rule 1/2/5/3b hold-triggering status sets for
    every Status value. Full output equivalence is NOT asserted: Rule 3
    (STAGE_SUCCESS_STATUSES) advances the pipeline stage rather than returning
    a fixed QueueItemStatus -- salvage cannot do that (no live task to
    advance), so only BLOCKED_ON_USER-membership is compared, per R4.
    """
    from typing import get_args

    from cw.auto_dev_result import SALVAGE_HOLD_STATUSES, Status
    from cw.dispatch.routing import (
        PAUSED_FOR_USER_INPUT_STATUSES as _DISPATCH_PAUSED,
    )
    from cw.dispatch.routing import (
        SCOPE_GATED_APPROVAL_STATUSES as _DISPATCH_SCOPE_GATED,
    )
    from cw.dispatch.routing import STAGE_FAILURE_STATUSES as _DISPATCH_STAGE_FAILURE

    dispatch_hold_statuses = (
        _DISPATCH_SCOPE_GATED
        | _DISPATCH_PAUSED
        | _DISPATCH_STAGE_FAILURE
        | frozenset({"merge_pending"})
    )
    for status in get_args(Status):
        assert (status in SALVAGE_HOLD_STATUSES) == (
            status in dispatch_hold_statuses
        ), f"status={status!r}: salvage classifier vs dispatch Rule 1/2/5/3b diverge"


# Frozen time for backfill tests: session started_at is 60s before frozen now.
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
    return _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
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


def test_transcript_age_seconds_widens_to_nested_subagent_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1431: a fresh subagent transcript nested under a subdirectory (e.g.
    ``<uuid>/subagents/agent-x.jsonl``) must widen liveness too, not just a
    flat sibling -- the glob must be recursive."""
    from cw.reconcile import _transcript_age_seconds

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-nested-age"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    reg = _write_idle_transcript(home, worktree, filename="fake-short-id-sess.jsonl")
    reg_ts = (started_at + timedelta(seconds=60)).timestamp()
    os.utime(reg, (reg_ts, reg_ts))
    sib = _write_idle_transcript(
        home, worktree, filename="some-uuid/subagents/agent-abc.jsonl"
    )
    sib_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-nested-age-1", worktree, started_at)

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


class TestWorktreeDirtyByPath:
    """Unit tests for _worktree_dirty_by_path."""

    def test_returns_false_when_checked_out_branch_raises(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_worktree_dirty_by_path returns False when the internal call raises."""
        from cw.reconcile import _worktree_dirty_by_path

        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: (_ for _ in ()).throw(RuntimeError("git failure")),
        )
        assert _worktree_dirty_by_path("client-a", tmp_path / "wt") is False


class TestReadUnresolvedSubagentSpawn:
    """Unit tests for _read_unresolved_subagent_spawn (#1646).

    Fail-open in one direction only: every ambiguous outcome (no file, no key,
    corrupt JSON, wrong types, no path at all) must read False. Reporting a
    spawn as unresolved on ambiguous evidence would park healthy tickets.
    """

    @staticmethod
    def _write_context(worktree: Path, payload: object) -> None:
        (worktree / ".claude").mkdir(parents=True, exist_ok=True)
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_true_when_marker_present(self, tmp_path: Path) -> None:
        """unresolved_count > 0 → True."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        self._write_context(tmp_path, {"agent_spawn_stamp": {"unresolved_count": 1}})
        assert _read_unresolved_subagent_spawn(tmp_path) is True

    def test_false_when_count_is_zero(self, tmp_path: Path) -> None:
        """A resolved spawn (count back to 0) → False."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        self._write_context(tmp_path, {"agent_spawn_stamp": {"unresolved_count": 0}})
        assert _read_unresolved_subagent_spawn(tmp_path) is False

    def test_false_when_absent(self, tmp_path: Path) -> None:
        """A pre-v5 context with no stamp key at all → False (legacy shape)."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        self._write_context(tmp_path, {"schema_version": 4, "session_id": "x"})
        assert _read_unresolved_subagent_spawn(tmp_path) is False

    def test_false_when_stamp_is_not_a_dict(self, tmp_path: Path) -> None:
        """A stamp key holding a scalar → False, not a crash."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        self._write_context(tmp_path, {"agent_spawn_stamp": 3})
        assert _read_unresolved_subagent_spawn(tmp_path) is False

    def test_false_when_count_is_bool(self, tmp_path: Path) -> None:
        """``True`` is an int in Python — it must not read as a live count."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        self._write_context(tmp_path, {"agent_spawn_stamp": {"unresolved_count": True}})
        assert _read_unresolved_subagent_spawn(tmp_path) is False

    def test_fail_open_on_malformed_json(self, tmp_path: Path) -> None:
        """A corrupt cw-context.json → False, never raises."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        (tmp_path / ".claude").mkdir(parents=True)
        (tmp_path / HOOK_CONTEXT_RELATIVE_PATH).write_text(
            "{ truncated", encoding="utf-8"
        )
        assert _read_unresolved_subagent_spawn(tmp_path) is False

    def test_fail_open_on_non_dict_context(self, tmp_path: Path) -> None:
        """A context that parses to a list → False."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        self._write_context(tmp_path, [])
        assert _read_unresolved_subagent_spawn(tmp_path) is False

    def test_fail_open_on_missing_file(self, tmp_path: Path) -> None:
        """A worktree with no .claude/cw-context.json → False."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        assert _read_unresolved_subagent_spawn(tmp_path / "gone") is False

    def test_fail_open_on_missing_worktree_path(self) -> None:
        """worktree_path=None (USER-origin sessions) → False."""
        from cw.reconcile._shared import _read_unresolved_subagent_spawn

        assert _read_unresolved_subagent_spawn(None) is False


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
    return _make_daemon_session(
        id="test-locate",
        name="client-a/impl",
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


_TWO_LAYER_SURFACE_REF = "surf1234"
_TWO_LAYER_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _write_two_layer_sentinel_transcript(
    path: Path, status: str, ticket_id: str
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


def _write_two_layer_no_sentinel_transcript(path: Path) -> None:
    """Write a JSONL with no sentinel framing."""
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "narrative only, no sentinel"}],
        },
    }
    path.write_text(json.dumps(record) + "\n")


def _mk_two_layer_fallback_session(worktree: Path, csid: str | None = None) -> Session:
    return _make_daemon_session(
        id="892-sess",
        name="client-a/auto-dev/892",
        worktree_path=worktree,
        surface_ref=_TWO_LAYER_SURFACE_REF,
        claude_session_id=csid,
        started_at=_TWO_LAYER_STARTED_AT,
    )


class TestParseAnySentinelFromTranscript:
    """Two-layer transcript search: csid-exact (Layer 1) then surface_ref (Layer 2).

    Regression for GitHub #892: a REVIEW worker that emits review_pending_approval
    and then spawns fanout subagents gets a new csid (V2) backfilled from the
    daemon roster.  The sentinel lives in the pre-resume V1 transcript; Layer 2
    must fall through to find it when V1 does not match the csid-exact path.
    """

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
        _write_two_layer_no_sentinel_transcript(v2_path)

        # V1: surface_ref transcript (matches "surf1234*.jsonl" glob, has sentinel)
        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-v1original.jsonl"
        _write_two_layer_sentinel_transcript(v1_path, "review_pending_approval", "892")

        sess = _mk_two_layer_fallback_session(worktree, csid=v2_csid)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "review_pending_approval"
        assert csid_stem == f"{_TWO_LAYER_SURFACE_REF}-v1original"

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
        _write_two_layer_no_sentinel_transcript(v2_path)

        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-v1-also-none.jsonl"
        _write_two_layer_no_sentinel_transcript(v1_path)

        sess = _mk_two_layer_fallback_session(worktree, csid=v2_csid)
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
        _write_two_layer_sentinel_transcript(
            v2_path, "plan_pending_approval", "892-plan"
        )

        # surface_ref transcript also has a sentinel (review_pending) — should NOT win
        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-v1-review.jsonl"
        _write_two_layer_sentinel_transcript(v1_path, "review_pending_approval", "892")

        sess = _mk_two_layer_fallback_session(worktree, csid=v2_csid)
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

        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-original.jsonl"
        _write_two_layer_sentinel_transcript(
            v1_path, "plan_pending_approval", "892-plan"
        )

        sess = _mk_two_layer_fallback_session(worktree, csid=None)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "plan_pending_approval"
        assert csid_stem == f"{_TWO_LAYER_SURFACE_REF}-original"


class TestSalvageTerminalResultTwoLayerFallback:
    """GitHub #1353 defect (a): _salvage_terminal_result must use the same
    two-layer transcript search as _parse_any_sentinel_from_transcript.

    Prior to the fix, _salvage_terminal_result called _locate_session_transcript
    (single lookup: csid-exact OR surface_ref-newest, no fallback between them)
    directly, so it missed a real terminal sentinel whenever the csid-exact
    transcript existed but was sentinel-less (or absent) and the sentinel
    actually lived in the surface_ref transcript. This mirrors the #892 fix
    already applied to _parse_any_sentinel_from_transcript.
    """

    def test_v2_csid_no_sentinel_v1_surface_ref_has_terminal_sentinel_returns_v1(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression for #1353: csid (V2) transcript exists but carries no
        sentinel; surface_ref (V1) transcript carries review_pending_approval.
        _salvage_terminal_result must fall through to Layer 2 and find it."""
        from cw.reconcile._shared import _salvage_terminal_result

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1353-layer2"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v2_csid = "v2-resumed-csid-1353"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        _write_two_layer_no_sentinel_transcript(v2_path)

        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-v1original.jsonl"
        _write_two_layer_sentinel_transcript(v1_path, "review_pending_approval", "1353")

        sess = _mk_two_layer_fallback_session(worktree, csid=v2_csid)
        result = _salvage_terminal_result(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "review_pending_approval"
        assert csid_stem == f"{_TWO_LAYER_SURFACE_REF}-v1original"

    def test_csid_transcript_with_terminal_sentinel_wins_over_surface_ref(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Layer 1 wins (unchanged behavior) — guards the delegation doesn't
        regress the already-working case."""
        from cw.reconcile._shared import _salvage_terminal_result

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1353-layer1-wins"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v2_csid = "v2-has-sentinel-1353"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        _write_two_layer_sentinel_transcript(v2_path, "merge_gate_blocked", "1353-mgb")

        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-v1-review.jsonl"
        _write_two_layer_sentinel_transcript(v1_path, "review_pending_approval", "1353")

        sess = _mk_two_layer_fallback_session(worktree, csid=v2_csid)
        result = _salvage_terminal_result(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "merge_gate_blocked"
        assert csid_stem == v2_csid

    def test_intermediate_status_in_fallback_transcript_still_excluded(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """csid transcript has no sentinel; surface_ref transcript carries
        stage_complete (INTERMEDIATE_ADVANCE_STATUSES, NOT SALVAGE_TERMINAL).
        _salvage_terminal_result's own status filter must still apply on top
        of the shared two-layer walk."""
        from cw.reconcile._shared import _salvage_terminal_result

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1353-intermediate"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v2_csid = "v2-no-sentinel-1353"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        _write_two_layer_no_sentinel_transcript(v2_path)

        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-v1-stage-complete.jsonl"
        _write_two_layer_sentinel_transcript(v1_path, "stage_complete", "1353-stage")

        sess = _mk_two_layer_fallback_session(worktree, csid=v2_csid)
        result = _salvage_terminal_result(sess)

        assert result is None

    def test_csid_absent_surface_ref_only_terminal_sentinel_found(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No claude_session_id set, surface_ref-only transcript carries
        no_op. Layer-2-only path still works after delegation."""
        from cw.reconcile._shared import _salvage_terminal_result

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1353-no-csid"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-original.jsonl"
        _write_two_layer_sentinel_transcript(v1_path, "no_op", "1353-noop")

        sess = _mk_two_layer_fallback_session(worktree, csid=None)
        result = _salvage_terminal_result(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "no_op"
        assert csid_stem == f"{_TWO_LAYER_SURFACE_REF}-original"

    def test_neither_transcript_has_sentinel_returns_none(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both transcripts empty of any sentinel framing -> None."""
        from cw.reconcile._shared import _salvage_terminal_result

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1353-neither"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v2_csid = "v2-neither-1353"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        _write_two_layer_no_sentinel_transcript(v2_path)

        v1_path = project_dir / f"{_TWO_LAYER_SURFACE_REF}-neither.jsonl"
        _write_two_layer_no_sentinel_transcript(v1_path)

        sess = _mk_two_layer_fallback_session(worktree, csid=v2_csid)
        result = _salvage_terminal_result(sess)

        assert result is None


# ---------------------------------------------------------------------------
# #1487 — salvaged sentinels get their self-reported scope verified against
# real git facts before any consumer sees them.
# ---------------------------------------------------------------------------


def _write_scope_guard_clients_yaml(
    tmp_config_dir: Path, *, default_branch: str
) -> None:
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n"
        "  client-a:\n"
        "    workspace_path: /tmp/ws-scope-guard\n"
        f"    default_branch: {default_branch}\n"
    )


def _parse_scope_guard_sentinel(
    home: Path, worktree: Path, payload: dict[str, Any]
) -> AutoDevResult:
    from cw.reconcile._shared import _parse_any_sentinel_from_transcript

    sess = _mk_headless_daemon_session(
        "scope-guard", worktree, datetime(2026, 1, 1, tzinfo=UTC)
    )
    _write_salvage_transcript(home, worktree, "claude-uuid-scope", payload)
    parsed = _parse_any_sentinel_from_transcript(sess)
    assert parsed is not None
    result, _csid = parsed
    assert isinstance(result, AutoDevResult)
    return result


def test_salvaged_sentinel_scope_corrected_against_git_facts(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_git_repo: Any,
) -> None:
    """Inflated self-report from a stale merge-base → corrected to real numbers."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = _make_stale_base_repo(make_git_repo, "wt-scope-guard")

    result = _parse_scope_guard_sentinel(
        home, worktree, _inflate_scope(_stage_complete_payload())
    )

    assert result.scope.files == SCOPE_GUARD_FILES
    assert result.scope.lines_actual == SCOPE_GUARD_LINES
    # Untouched fields ride through unchanged.
    assert result.scope.lines_estimate == 60
    assert result.scope.tier == "small"
    assert result.status == "stage_complete"


def test_salvaged_sentinel_scope_honours_client_default_branch(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_git_repo: Any,
) -> None:
    """The measurement resolves the client's configured default_branch, not 'main'."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = _make_stale_base_repo(
        make_git_repo, "wt-scope-trunk", default_branch="trunk"
    )
    _write_scope_guard_clients_yaml(tmp_config_dir, default_branch="trunk")

    result = _parse_scope_guard_sentinel(
        home, worktree, _inflate_scope(_stage_complete_payload())
    )

    assert result.scope.files == SCOPE_GUARD_FILES
    assert result.scope.lines_actual == SCOPE_GUARD_LINES


def test_salvaged_sentinel_scope_unknown_client_key_logs_warning(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_git_repo: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A well-formed clients.yaml missing the session's client key now warns.

    Regression pin for #1487's fix loop: before the shared
    resolve_scope_guard_default_branch helper existed, _verify_salvaged_scope
    resolved this case via a silent dict.get() miss with no WARNING, diverging
    from the Stop-hook family's logged get_client()-raises path.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = _make_stale_base_repo(make_git_repo, "wt-scope-unknown-client")
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n  some-other-client:\n    workspace_path: /tmp/ws-other\n"
    )

    with caplog.at_level(logging.WARNING, logger="cw.worktree"):
        result = _parse_scope_guard_sentinel(
            home, worktree, _inflate_scope(_stage_complete_payload())
        )

    assert result.scope.files == SCOPE_GUARD_FILES
    assert result.scope.lines_actual == SCOPE_GUARD_LINES
    assert "scope_verification_client_unresolved" in caplog.text
    assert "client=client-a" in caplog.text


def test_salvaged_sentinel_scope_unverifiable_worktree_left_alone(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-git worktree → measurement unavailable, self-report preserved."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-scope-nogit"
    worktree.mkdir()

    result = _parse_scope_guard_sentinel(
        home, worktree, _inflate_scope(_stage_complete_payload())
    )

    assert result.scope.files == 18
    assert result.scope.lines_actual == 1567


def test_salvaged_sentinel_scope_survives_unresolvable_client_config(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_git_repo: Any,
) -> None:
    """A broken clients.yaml must not lose the sentinel — fall back to 'main'.

    _verify_salvaged_scope now resolves default_branch via the shared
    cw.worktree.resolve_scope_guard_default_branch helper (#1487 fix loop),
    which calls cw.worktree.load_effective_clients directly rather than the
    reconcile-cluster _deps indirection.
    """
    from cw import worktree as wt_mod
    from cw.exceptions import CwError

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = _make_stale_base_repo(make_git_repo, "wt-scope-badcfg")

    def _boom() -> dict[str, ClientConfig]:
        msg = "clients.yaml is unreadable"
        raise CwError(msg)

    monkeypatch.setattr(wt_mod, "load_effective_clients", _boom)

    result = _parse_scope_guard_sentinel(
        home, worktree, _inflate_scope(_stage_complete_payload())
    )

    assert result.scope.files == SCOPE_GUARD_FILES
    assert result.scope.lines_actual == SCOPE_GUARD_LINES
