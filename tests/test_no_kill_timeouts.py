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
)

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


def _run_liveness(state: CwState) -> None:
    record_session_liveness_changes(
        state,
        now=_NOW,
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
