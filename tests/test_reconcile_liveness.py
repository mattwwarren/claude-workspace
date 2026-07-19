"""Unit tests for cw.reconcile.liveness (RFC 0008 W2, GitHub #1001).

Covers the transcript-staleness bucket sweep: floor-suppression
classification, gating, latch/edge-detect semantics, event payload shape,
and stage resolution via TicketTask (not Session.stage).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.models import (
    CwState,
    LivenessBucket,
    OrchestratorConfig,
    OrchestratorEventType,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile.liveness import (
    _classify_liveness_bucket,
    _detect_liveness_candidates,
    record_session_liveness_changes,
)
from tests.conftest import _make_daemon_session, _write_idle_transcript

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)


def _mk_liveness_session(
    *,
    tmp_path: Path,
    surface_ref: str = "fake-short-id",
    ticket_id: str = "T-1",
) -> tuple[object, Path]:
    """Build a DAEMON session with a real worktree_path for transcript lookup.

    Returns (session, worktree_path). Caller writes and stamps the transcript.
    """
    worktree = tmp_path / "wt"
    sess = _make_daemon_session(
        surface_ref=surface_ref,
        worktree_path=worktree,
        started_at=_STARTED_AT,
        name=f"client-a/auto-dev/{ticket_id}",
    )
    return sess, worktree


def _stamp_transcript_stale_minutes(
    home: Path,
    worktree: Path,
    *,
    stale_minutes: float,
    surface_ref: str = "fake-short-id",
) -> Path:
    """Write a transcript and set its mtime so (_NOW - mtime) == stale_minutes."""
    transcript = _write_idle_transcript(
        home, worktree, filename=f"{surface_ref}-sess.jsonl"
    )
    mtime_dt = _NOW - timedelta(minutes=stale_minutes)
    ts = mtime_dt.timestamp()
    os.utime(str(transcript), (ts, ts))
    return transcript


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so transcript lookup finds our written .jsonl files."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


# ---------------------------------------------------------------------------
# Floor-suppression classification (R6 worked examples + generalization)
# ---------------------------------------------------------------------------


def test_impl_stage_32m_stale_classifies_live_no_emit(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """impl@32m, floor=35 → LIVE, no candidate (below the per-stage floor)."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=32)
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.IMPL)
    config = OrchestratorConfig(liveness_first_bucket_by_stage={Stage.IMPL: 35})

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    assert candidates == []
    assert sess.liveness_bucket == LivenessBucket.LIVE


def test_impl_stage_36m_stale_first_crossing_emits_stale_15m(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """impl@36m, floor=35 → STALE_15M (not stale_30m); one event emitted."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=36)
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.IMPL)
    config = OrchestratorConfig(liveness_first_bucket_by_stage={Stage.IMPL: 35})

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].old_bucket == LivenessBucket.LIVE
    assert candidates[0].new_bucket == LivenessBucket.STALE_15M
    assert sess.liveness_bucket == LivenessBucket.STALE_15M

    from cw.events import read_events

    events = read_events(
        consumer="test-liveness-36m",
        event_types=[OrchestratorEventType.SESSION_LIVENESS_CHANGED],
    )
    assert len(events) == 1
    assert events[0].payload["old_bucket"] == "live"
    assert events[0].payload["new_bucket"] == "stale_15m"


def test_impl_stage_ascending_30_to_45_never_emits_stale_30m(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """impl staleness ascending [30,34,38,42,44]m → never classifies stale_30m."""
    config = OrchestratorConfig(liveness_first_bucket_by_stage={Stage.IMPL: 35})
    for stale_minutes in (30, 34, 38, 42, 44):
        bucket = _classify_liveness_bucket(
            float(stale_minutes), stage=Stage.IMPL, config=config
        )
        assert bucket != LivenessBucket.STALE_30M, stale_minutes


def test_default_stage_boundaries_unchanged(tmp_config_dir: Path) -> None:
    """No per-stage override: 14/15/29/30/44/45m → live/15/15/30/30/45."""
    config = OrchestratorConfig()
    expected = {
        14: LivenessBucket.LIVE,
        15: LivenessBucket.STALE_15M,
        29: LivenessBucket.STALE_15M,
        30: LivenessBucket.STALE_30M,
        44: LivenessBucket.STALE_30M,
        45: LivenessBucket.STALE_45M,
    }
    for stale_minutes, want in expected.items():
        got = _classify_liveness_bucket(
            float(stale_minutes), stage=Stage.PLAN, config=config
        )
        assert got == want, (stale_minutes, got)


# ---------------------------------------------------------------------------
# Latch / edge-detect semantics
# ---------------------------------------------------------------------------


def test_bucket_crossing_emits_exactly_once_latch_no_refire(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """tick1 crosses+emits; tick2 same band → no re-emit."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=20)
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.PLAN)
    config = OrchestratorConfig()

    first = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )
    assert len(first) == 1
    assert sess.liveness_bucket == LivenessBucket.STALE_15M

    second = record_session_liveness_changes(
        state,
        now=_NOW + timedelta(minutes=1),
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )
    assert second == []
    assert sess.liveness_bucket == LivenessBucket.STALE_15M


def test_recovery_edge_back_to_live_emits_once(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """Persisted stale_30m; transcript freshens → one emit back to LIVE."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    sess.liveness_bucket = LivenessBucket.STALE_30M
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=1)
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.PLAN)
    config = OrchestratorConfig()

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].old_bucket == LivenessBucket.STALE_30M
    assert candidates[0].new_bucket == LivenessBucket.LIVE
    assert sess.liveness_bucket == LivenessBucket.LIVE


# ---------------------------------------------------------------------------
# Gating (RFC 0008 W2 round-1, R2)
# ---------------------------------------------------------------------------


def test_gating_skips_non_daemon_session(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """USER-origin session is never classified, regardless of staleness."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    sess.origin = SessionOrigin.USER
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_gating_skips_non_live_status(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """COMPLETED session is never classified, regardless of staleness."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    sess.status = SessionStatus.COMPLETED
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_gating_skips_surface_ref_not_in_native_live(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """surface_ref absent from native_live (phantom) → never classified here."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live=set(),
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_gating_skips_when_transcript_not_located(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """No transcript on disk → skip classification this tick (fail-open)."""
    sess, _worktree = _mk_liveness_session(tmp_path=tmp_path)
    # Deliberately do not write a transcript.
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


# ---------------------------------------------------------------------------
# Event payload shape + stage resolution
# ---------------------------------------------------------------------------


def test_event_payload_shape_matches_spec(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """Exact key set; stale_minutes is float; correlation_id == ticket_id."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=20)
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.PLAN)
    config = OrchestratorConfig()

    record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    from cw.events import read_events

    events = read_events(
        consumer="test-liveness-payload-shape",
        event_types=[OrchestratorEventType.SESSION_LIVENESS_CHANGED],
    )
    assert len(events) == 1
    event = events[0]
    assert event.correlation_id == "T-1"
    assert set(event.payload.keys()) == {
        "session_id",
        "ticket_id",
        "client",
        "stage",
        "old_bucket",
        "new_bucket",
        "stale_minutes",
    }
    assert isinstance(event.payload["stale_minutes"], float)


def test_stage_resolved_via_task_by_ticket_not_session_stage(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """Session.stage is dormant; TicketTask.stage drives the per-stage floor."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    sess.stage = Stage.REVIEW  # dormant field — must NOT be consulted
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=20)
    state = CwState(sessions=[sess])
    # TicketTask.stage says IMPL with a 35m floor; if Session.stage (REVIEW,
    # no override -> floor 15) were used instead, 20m would already cross
    # into stale_15m. Confirm the IMPL floor (35m) is honored instead --
    # 20m stays LIVE.
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.IMPL)
    config = OrchestratorConfig(liveness_first_bucket_by_stage={Stage.IMPL: 35})

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    assert candidates == []


def test_liveness_first_bucket_by_stage_custom_override_respected(
    tmp_config_dir: Path,
) -> None:
    """Floor generalizes beyond impl -- e.g. review: 5 lowers the entry point."""
    config = OrchestratorConfig(liveness_first_bucket_by_stage={Stage.REVIEW: 5})

    below_floor = _classify_liveness_bucket(4.0, stage=Stage.REVIEW, config=config)
    at_floor = _classify_liveness_bucket(5.0, stage=Stage.REVIEW, config=config)

    assert below_floor == LivenessBucket.LIVE
    assert at_floor == LivenessBucket.STALE_15M


def test_liveness_bucket_reflects_widened_transcript(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """#1283: a stale registered transcript but a fresh subagent sibling in the
    same project dir classifies LIVE, not a stale bucket (widened mtime lookup)."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    sess.liveness_bucket = LivenessBucket.STALE_45M
    # Registered transcript (surface_ref-prefixed), long stale.
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=90)
    # Fresh sibling subagent transcript (own session id, NOT surface_ref-prefixed).
    sib = _write_idle_transcript(home, worktree, filename="subagent-fresh.jsonl")
    sib_ts = (_NOW - timedelta(minutes=1)).timestamp()
    os.utime(str(sib), (sib_ts, sib_ts))

    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.IMPL)
    config = OrchestratorConfig()

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].old_bucket == LivenessBucket.STALE_45M
    assert candidates[0].new_bucket == LivenessBucket.LIVE
