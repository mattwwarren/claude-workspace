"""Cross-cutting regression suite for the process-kill-timeout removal.

Pins the three surfaces the removal changed outside the sweep packages:

1. ``OrchestratorConfig`` — the timeout knobs are gone, and legacy YAML that
   still carries them loads with a warning instead of crashing.
2. The Stop hook — a sentinel-less headless Stop defers unconditionally; the
   budget-expiry TIMED_OUT path no longer exists.
3. The liveness sweep's operator distress signal — the *signal class* the
   destructive timers used to provide survives as a multi-dimensional,
   signal-only ``SESSION_NEEDS_ATTENTION`` (roster presence + transcript
   staleness + sentinel absence + subagent-await), with zero disposition.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.events import read_events
from cw.models import (
    CwState,
    LivenessBucket,
    OrchestratorConfig,
    OrchestratorEventType,
)
from cw.reconcile import _deps
from cw.reconcile._shared import _SESSION_UNRESPONSIVE_REASON
from cw.reconcile.liveness import record_session_liveness_changes
from tests._reconcile_helpers import (
    _mk_headless_daemon_session,
    _shipped_salvage_payload,
    _write_transcript_records,
)
from tests.conftest import _invoke_hook_command, _write_hook_context_file
from tests.test_cli_agent_spawn_stamp import _PRE_PAYLOAD, _payload

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Config surface
# ---------------------------------------------------------------------------


def test_removed_timeout_config_keys_are_stripped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        config = OrchestratorConfig.model_validate(
            {
                "headless_timeout_by_tier": {"small": 1800},
                "idle_watchdog_seconds": 900,
                "park_veto_cap": 3,
                "tick_interval_seconds": 45,
            }
        )

    assert config.tick_interval_seconds == 45
    assert not hasattr(config, "headless_timeout_by_tier")
    assert not hasattr(config, "idle_watchdog_seconds")
    assert not hasattr(config, "park_veto_cap")
    assert any("removed timeout setting" in r.message for r in caplog.records)


def test_config_has_no_timeout_fields() -> None:
    config = OrchestratorConfig()
    for removed in (
        "headless_timeout_by_tier",
        "headless_timeout_by_stage",
        "idle_watchdog_by_tier",
        "idle_watchdog_by_stage",
        "idle_watchdog_seconds",
        "idle_retry_cap_by_tier",
        "stalled_retry_cap_by_tier",
        "idle_confirm_observations",
        "park_veto_cap",
        "salvage_skip_attention_threshold",
    ):
        assert removed not in type(config).model_fields


# ---------------------------------------------------------------------------
# 2. Stop hook
# ---------------------------------------------------------------------------


def test_stop_hook_no_sentinel_always_defers() -> None:
    from cw.cli import stop_hook

    assert stop_hook._handle_headless_no_sentinel() is True
    assert not hasattr(stop_hook, "_record_headless_timeout")


# ---------------------------------------------------------------------------
# 3. Liveness distress signal
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def push_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _deps,
        "fire_push_notification",
        lambda name, client: calls.append((name, client)),
    )
    return calls


def _stale_transcript(home: Path, worktree: Path, *, stale_minutes: float) -> None:
    from tests.conftest import _write_idle_transcript

    transcript = _write_idle_transcript(home, worktree)
    ts = (_NOW - timedelta(minutes=stale_minutes)).timestamp()
    os.utime(str(transcript), (ts, ts))


def _run_liveness(state: CwState, *, now: datetime = _NOW) -> None:
    record_session_liveness_changes(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )


def _distress_events() -> list[dict[str, object]]:
    return [
        dict(e.payload)
        for e in read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        if e.payload.get("paused_status") == _SESSION_UNRESPONSIVE_REASON
    ]


def test_top_bucket_crossing_emits_distress_signal_without_disposition(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=46)
    state = CwState(sessions=[sess])

    _run_liveness(state)

    assert sess.liveness_bucket is LivenessBucket.STALE_45M
    events = _distress_events()
    assert len(events) == 1
    assert events[0]["session_id"] == sess.id
    assert "transcript flat" in str(events[0]["breadcrumbs"])
    assert push_calls == [(sess.name, sess.client)]
    # Signal-only: the session itself is untouched.
    assert sess.status.value == "active"
    assert sess.last_result is None


def test_distress_signal_is_edge_triggered(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=46)
    state = CwState(sessions=[sess])

    _run_liveness(state)
    _run_liveness(state)  # same staleness — bucket already latched

    assert len(_distress_events()) == 1
    assert len(push_calls) == 1


def test_no_distress_when_session_already_emitted_sentinel(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    sess.last_result = _shipped_salvage_payload()
    _stale_transcript(home, tmp_path / "wt", stale_minutes=46)
    state = CwState(sessions=[sess])

    _run_liveness(state)

    # The bucket still latches (observability), but no operator page fires —
    # the quietness is explained by the recorded terminal result.
    assert sess.liveness_bucket is LivenessBucket.STALE_45M
    assert _distress_events() == []
    assert push_calls == []


def _balanced_agent_pairs(n: int, *, base_time: datetime) -> list[dict[str, object]]:
    """Build *n* resolved Agent tool_use/tool_result pairs (#1969, instance #9).

    ``PostToolUse:Agent`` fires at launch-return, not subagent completion
    (docs/spikes/claude-bg-wakeup-drop-findings.md, #1947), so a real parent
    transcript shows a *resolved* pair for a subagent spawn that is, in
    reality, still outstanding. A transcript-tail pairing check sees nothing
    pending here by construction.
    """
    records: list[dict[str, object]] = []
    for i in range(n):
        ts = (base_time + timedelta(seconds=i)).isoformat()
        records.append(
            {
                "type": "assistant",
                "timestamp": ts,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": f"toolu_{i}", "name": "Agent"}
                    ],
                },
            }
        )
        records.append(
            {
                "type": "user",
                "timestamp": ts,
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}"}]
                },
            }
        )
    return records


def test_no_distress_when_agent_spawn_stamp_is_outstanding(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    """Real-incident shape (#1969, instance #9): the parent transcript shows N
    *resolved* Agent tool_use/tool_result pairs at its tail, so a transcript-
    pairing liveness check sees nothing pending and would false-fire distress.
    The ``agent_spawn_stamp`` counter -- driven off the Stop hook's own
    ``background_tasks`` snapshot (#1947) -- still shows an outstanding
    subagent spawn; distress must not fire.

    Post-#2012 that suppression is age-bounded
    (``fix_loop_await_deadline_minutes``), and this case stays on the
    suppressed side because the real ``agent-spawn-pre`` hook stamps
    ``last_stamped_at`` with wall-clock now while the sweep runs at the frozen
    ``_NOW`` -- i.e. the spawn reads as freshly stamped, never as past its
    deadline. If this file's clock handling ever changes, that is the
    interaction to re-check; the deadline-crossing behavior itself is covered
    in ``tests/test_reconcile_liveness.py``.
    """
    worktree = tmp_path / "wt"
    sess = _mk_headless_daemon_session("T-1", worktree, _STARTED_AT)
    records = _balanced_agent_pairs(2, base_time=_NOW - timedelta(minutes=46))
    transcript = _write_transcript_records(home, worktree, records)
    stale_ts = (_NOW - timedelta(minutes=46)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    _write_hook_context_file(worktree)
    for _ in range(2):
        _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))
    state = CwState(sessions=[sess])

    _run_liveness(state)

    assert sess.liveness_bucket is LivenessBucket.STALE_45M
    assert _distress_events() == []
    assert push_calls == []


def test_distress_still_fires_when_agent_spawn_stamp_is_clear(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    """Positive control for the regression above: same resolved-pair transcript
    shape, but the ``agent_spawn_stamp`` counter is clear (no outstanding
    spawn). Distress must still fire -- the fix suppresses distress only on
    genuine evidence of an outstanding subagent, not unconditionally."""
    worktree = tmp_path / "wt"
    sess = _mk_headless_daemon_session("T-1", worktree, _STARTED_AT)
    records = _balanced_agent_pairs(2, base_time=_NOW - timedelta(minutes=46))
    transcript = _write_transcript_records(home, worktree, records)
    stale_ts = (_NOW - timedelta(minutes=46)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    _write_hook_context_file(worktree)  # unresolved_count starts at 0
    state = CwState(sessions=[sess])

    _run_liveness(state)

    assert sess.liveness_bucket is LivenessBucket.STALE_45M
    events = _distress_events()
    assert len(events) == 1
    assert "no pending subagent" in str(events[0]["breadcrumbs"])
    assert push_calls == [(sess.name, sess.client)]


def test_distress_fires_for_stale_synchronous_tool_use_with_no_agent_spawn_stamp(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    """A trailing *unmatched* synchronous tool_use (not an Agent spawn) with no
    ``agent_spawn_stamp`` entry at all is still classified as distress -- a
    genuinely hung synchronous tool call is not evidence of a pending
    subagent."""
    worktree = tmp_path / "wt"
    sess = _mk_headless_daemon_session("T-1", worktree, _STARTED_AT)
    record: dict[str, object] = {
        "type": "assistant",
        "timestamp": (_NOW - timedelta(minutes=46)).isoformat(),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu1", "name": "Bash"}],
        },
    }
    transcript = _write_transcript_records(home, worktree, [record])
    stale_ts = (_NOW - timedelta(minutes=46)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    state = CwState(sessions=[sess])

    _run_liveness(state)

    assert sess.liveness_bucket is LivenessBucket.STALE_45M
    events = _distress_events()
    assert len(events) == 1
    assert events[0]["session_id"] == sess.id


def test_no_distress_below_top_bucket(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=32)
    state = CwState(sessions=[sess])

    _run_liveness(state)

    assert sess.liveness_bucket is LivenessBucket.STALE_30M
    assert _distress_events() == []
    assert push_calls == []


# ---------------------------------------------------------------------------
# 4. Liveness re-fire cadence (#1858)
# ---------------------------------------------------------------------------


def test_distress_renotifies_after_debounce_interval_elapses(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=46)
    state = CwState(sessions=[sess])

    _run_liveness(state, now=_NOW)
    _run_liveness(state, now=_NOW + timedelta(minutes=61))

    assert len(_distress_events()) == 2
    assert len(push_calls) == 2


def test_distress_does_not_renotify_partway_through_interval(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=46)
    state = CwState(sessions=[sess])

    _run_liveness(state, now=_NOW)
    _run_liveness(state, now=_NOW + timedelta(minutes=30))

    assert len(_distress_events()) == 1
    assert len(push_calls) == 1


def test_renotify_marker_present_and_distinct_across_fires(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=46)
    state = CwState(sessions=[sess])

    _run_liveness(state, now=_NOW)
    _run_liveness(state, now=_NOW + timedelta(minutes=61))

    events = _distress_events()
    assert len(events) == 2
    markers = [e["renotify_marker"] for e in events]
    assert all(m is not None for m in markers)
    for marker in markers:
        assert isinstance(marker, str)
        datetime.fromisoformat(marker)
    assert markers[0] != markers[1]


def test_renotify_suppressed_once_terminal_sentinel_lands(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=46)
    state = CwState(sessions=[sess])

    _run_liveness(state, now=_NOW)
    sess.last_result = _shipped_salvage_payload()

    _run_liveness(state, now=_NOW + timedelta(minutes=61))

    assert len(_distress_events()) == 1


def test_liveness_attention_next_eligible_at_cleared_on_recovery(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    sess.liveness_bucket = LivenessBucket.STALE_45M
    sess.liveness_attention_next_eligible_at = _NOW + timedelta(minutes=60)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=1)
    state = CwState(sessions=[sess])

    _run_liveness(state)

    assert sess.liveness_bucket is LivenessBucket.LIVE
    assert sess.liveness_attention_next_eligible_at is None


def test_liveness_renotify_survives_dedup_terminal(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    from cw.cli.queues import _dedup_terminal

    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript(home, tmp_path / "wt", stale_minutes=46)
    state = CwState(sessions=[sess])

    _run_liveness(state, now=_NOW)
    _run_liveness(state, now=_NOW + timedelta(minutes=61))

    events = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(events) == 2
    assert len(_dedup_terminal(events)) == 2


# ---------------------------------------------------------------------------
# 5. Real-incident signature regression (#1889)
# ---------------------------------------------------------------------------
#
# claude --bg async-completion wakeups (background Bash, Agent-tool subagent)
# are not reliably delivered to the session -- an upstream claude-code
# defect, not cw-fixable (see docs/spikes/claude-bg-wakeup-drop-findings.md).
# Real captured transcripts for three incidents (#1801, #1838, #1751) all
# show the identical shape: a *resolved* async-dispatch tool_result, followed
# by ordinary end_turn text saying "waiting for X", then silence -- not a
# dangling tool_use. That leaves the same flat-transcript signature the
# liveness distress path already detects via an empty idle transcript; these
# two tests pin that the composed real signature (resolved tool_result +
# "waiting" text) is not accidentally treated as "session doing work" by any
# future change to the distress heuristic.


def _stale_transcript_with_text(
    home: Path, worktree: Path, assistant_text: str, *, stale_minutes: float
) -> None:
    from tests._reconcile_helpers import _write_idle_transcript_with_text

    transcript = _write_idle_transcript_with_text(home, worktree, assistant_text)
    ts = (_NOW - timedelta(minutes=stale_minutes)).timestamp()
    os.utime(str(transcript), (ts, ts))


def test_distress_fires_on_resolved_bg_bash_wait_signature(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    """#1801 real transcript shape: a resolved bg-Bash tool_result, then plain
    "waiting for it" end_turn text -- must still cross into the top-bucket
    distress signal like an empty idle transcript does."""
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript_with_text(
        home,
        tmp_path / "wt",
        "Waiting for that background command to finish (it should be "
        "near-instant — likely just slow shell startup).",
        stale_minutes=46,
    )
    state = CwState(sessions=[sess])

    _run_liveness(state)

    assert sess.liveness_bucket is LivenessBucket.STALE_45M
    events = _distress_events()
    assert len(events) == 1
    assert events[0]["session_id"] == sess.id


def test_distress_fires_on_resolved_subagent_wait_signature(
    tmp_config_dir: Path, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
) -> None:
    """#1838 real transcript shape: a resolved Agent-tool tool_result, then
    plain "waiting for it to finish" end_turn text -- same distress
    requirement as the bg-Bash channel above."""
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    _stale_transcript_with_text(
        home,
        tmp_path / "wt",
        "Fix-cycle 1 agent dispatched (`a30d3f56fd68cc778`) in an isolated "
        "worktree to apply the 6-item action list. Waiting for it to finish.",
        stale_minutes=46,
    )
    state = CwState(sessions=[sess])

    _run_liveness(state)

    assert sess.liveness_bucket is LivenessBucket.STALE_45M
    events = _distress_events()
    assert len(events) == 1
    assert events[0]["session_id"] == sess.id
