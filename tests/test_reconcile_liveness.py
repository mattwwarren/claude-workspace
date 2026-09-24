"""Unit tests for cw.reconcile.liveness (RFC 0008 W2, GitHub #1001).

Covers the transcript-staleness bucket sweep: floor-suppression
classification, gating, latch/edge-detect semantics, event payload shape,
and stage resolution via TicketTask (not Session.stage).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    CwState,
    LivenessBucket,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
    UsageLimitAct,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _FIX_LOOP_AWAIT_DEADLINE_EXCEEDED_REASON,
    _SESSION_UNRESPONSIVE_REASON,
    _STOPPED_WITHOUT_SENTINEL_REASON,
    _UNCONSUMED_QUEUE_NOTIFICATION_REASON,
    _USAGE_LIMITED_MID_TURN_REASON,
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


# ---------------------------------------------------------------------------
# Deadline-bounded distress suppression (#2012)
#
# Before #2012 an outstanding agent_spawn_stamp suppressed the distress signal
# unconditionally and forever: "awaiting a subagent" was, by design, treated as
# definitionally healthy with no age bound at all. These tests pin the bound.
# ---------------------------------------------------------------------------


def _write_spawn_stamp(
    worktree: Path, *, unresolved_count: int, stamped_at: datetime | None
) -> None:
    """Write an ``agent_spawn_stamp`` into *worktree*'s cw-context.json.

    Mirrors ``TestReadUnresolvedSubagentSpawn._write_context``'s shape in
    ``tests/test_reconcile_shared_sentinels.py`` — the same on-disk payload
    ``cw agent-spawn-pre`` produces, written directly so the stamp age is
    controllable relative to the frozen ``_NOW``.
    """
    (worktree / ".claude").mkdir(parents=True, exist_ok=True)
    payload = {
        "agent_spawn_stamp": {
            "unresolved_count": unresolved_count,
            "last_stamped_at": stamped_at.isoformat() if stamped_at else None,
        }
    }
    (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _events_of(event_type: OrchestratorEventType) -> list[dict[str, object]]:
    """Payloads of every recorded event of *event_type* (#2135).

    Generalized from the former attention-only reader so the #2135
    suppression tests can assert on ``session.liveness_changed`` too without a
    second near-duplicate reader. The consumer name is derived from the event
    type so the two readers keep independent offsets.
    """
    from cw.events import read_events

    return [
        dict(e.payload)
        for e in read_events(
            consumer=f"test-liveness-{event_type.value}",
            event_types=[event_type],
        )
    ]


def test_spawn_within_deadline_still_suppresses_distress(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """Regression guard: a fresh outstanding spawn keeps today's suppression.

    The bound is a bound, not a removal — a session genuinely awaiting a
    subagent it dispatched 5 minutes ago must not page the operator.
    """
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    _write_spawn_stamp(
        worktree, unresolved_count=1, stamped_at=_NOW - timedelta(minutes=5)
    )
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.PLAN)
    config = OrchestratorConfig(fix_loop_await_deadline_minutes=30)

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].new_bucket == LivenessBucket.STALE_45M
    assert candidates[0].distress is False
    assert _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []


def test_spawn_past_deadline_fires_named_attention_signal(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """The literal #2012 requirement: the deadline fires and names what failed.

    An unresolved subagent spawn older than ``fix_loop_await_deadline_minutes``
    no longer suppresses distress; the SESSION_NEEDS_ATTENTION it emits carries
    the discriminating ``fix_loop_await_deadline_exceeded`` paused_status
    (not generic ``session_unresponsive``) and a breadcrumb naming both the
    elapsed minutes and the deadline.
    """
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    _write_spawn_stamp(
        worktree, unresolved_count=1, stamped_at=_NOW - timedelta(minutes=55)
    )
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.PLAN)
    config = OrchestratorConfig(fix_loop_await_deadline_minutes=30)

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].distress is True
    assert candidates[0].spawn_deadline_minutes == 30
    events = _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(events) == 1
    assert events[0]["paused_status"] == _FIX_LOOP_AWAIT_DEADLINE_EXCEEDED_REASON
    breadcrumbs = str(events[0]["breadcrumbs"])
    assert "55m" in breadcrumbs
    assert "30m" in breadcrumbs
    assert "subagent" in breadcrumbs


def test_no_spawn_stamp_keeps_generic_unresponsive_reason(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A stale session with no outstanding spawn keeps the pre-#2012 reason."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.PLAN)

    record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={"T-1": task},
    )

    events = _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(events) == 1
    assert events[0]["paused_status"] == _SESSION_UNRESPONSIVE_REASON


def test_terminal_sentinel_still_suppresses_past_deadline(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """Sentinel precedence is preserved: a completed session never pages."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    sess.last_result = {"status": "shipped"}
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    _write_spawn_stamp(
        worktree, unresolved_count=1, stamped_at=_NOW - timedelta(minutes=55)
    )
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.PLAN)
    config = OrchestratorConfig(fix_loop_await_deadline_minutes=30)

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"T-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].distress is False
    assert _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []


def test_detect_liveness_candidates_populates_dangling_tool_use_field(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A top-bucket, no-sentinel, no-spawn-stamp session with a dangling Bash
    call populates LivenessCandidate.dangling_tool_use (#1482)."""
    from cw.reconcile._shared import DanglingToolUseEvidence
    from tests._reconcile_helpers import _write_transcript_records

    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    record: dict[str, object] = {
        "type": "assistant",
        "timestamp": (_NOW - timedelta(minutes=60)).isoformat(),
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Bash",
                    "input": {"command": "pytest tests/"},
                }
            ],
        },
    }
    transcript = _write_transcript_records(home, worktree, [record])
    stale_ts = (_NOW - timedelta(minutes=60)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].new_bucket == LivenessBucket.STALE_45M
    assert candidates[0].dangling_tool_use == DanglingToolUseEvidence(
        tool_name="Bash", command_snippet="pytest tests/"
    )


def test_detect_liveness_candidates_leaves_dangling_tool_use_none_below_top_bucket(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """The field is only computed under distress_base (top bucket) -- no
    wasted scan / no premature signal below it (#1482)."""
    from tests._reconcile_helpers import _write_transcript_records

    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    record: dict[str, object] = {
        "type": "assistant",
        "timestamp": (_NOW - timedelta(minutes=32)).isoformat(),
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Bash",
                    "input": {"command": "pytest tests/"},
                }
            ],
        },
    }
    transcript = _write_transcript_records(home, worktree, [record])
    stale_ts = (_NOW - timedelta(minutes=32)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].new_bucket == LivenessBucket.STALE_30M
    assert candidates[0].dangling_tool_use is None


# ---------------------------------------------------------------------------
# #2251: unconsumed queue-operation enqueue notification at the transcript tail
# ---------------------------------------------------------------------------

_BG_DONE_TEXT = "Background command ci-local.sh completed (exit code 0)"


def _write_unconsumed_queue_transcript(
    home: Path, worktree: Path, *, stale_minutes: float, dangling_bash: bool = False
) -> None:
    """Write a transcript ending in an unconsumed queue-operation enqueue.

    With *dangling_bash*, an unresolved Bash tool_use precedes the enqueue
    record, so both #1482's and #2251's detectors would match it.
    """
    from tests._reconcile_helpers import (
        _notification_record,
        _write_transcript_records,
    )

    stale_at = _NOW - timedelta(minutes=stale_minutes)
    block: dict[str, object] = (
        {
            "type": "tool_use",
            "id": "tu1",
            "name": "Bash",
            "input": {"command": "pytest tests/"},
        }
        if dangling_bash
        else {"type": "text", "text": "running gates"}
    )
    records: list[dict[str, object]] = [
        {
            "type": "assistant",
            "timestamp": stale_at.isoformat(),
            "message": {"role": "assistant", "content": [block]},
        },
        _notification_record(_BG_DONE_TEXT, kind="queue-operation"),
    ]
    transcript = _write_transcript_records(home, worktree, records)
    os.utime(str(transcript), (stale_at.timestamp(), stale_at.timestamp()))


def test_detect_liveness_candidates_populates_unconsumed_queue_notification_field(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A top-bucket, no-sentinel, no-spawn-stamp session whose transcript tail
    is a queue-operation enqueue populates unconsumed_queue_notification
    (#2251)."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _write_unconsumed_queue_transcript(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].new_bucket == LivenessBucket.STALE_45M
    assert candidates[0].unconsumed_queue_notification == _BG_DONE_TEXT
    assert candidates[0].dangling_tool_use is None


def test_detect_liveness_candidates_leaves_unconsumed_queue_none_below_top_bucket(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """Only computed under distress_base (top bucket) (#2251)."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _write_unconsumed_queue_transcript(home, worktree, stale_minutes=32)
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].new_bucket == LivenessBucket.STALE_30M
    assert candidates[0].unconsumed_queue_notification is None


def test_unconsumed_queue_notification_takes_priority_over_dangling_tool_use(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A dangling Bash tool_use followed by a trailing enqueue record reports
    only the more specific #2251 signal; the #1482 scan is skipped."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _write_unconsumed_queue_transcript(
        home, worktree, stale_minutes=60, dangling_bash=True
    )
    state = CwState(sessions=[sess])

    candidates = _detect_liveness_candidates(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].unconsumed_queue_notification == _BG_DONE_TEXT
    assert candidates[0].dangling_tool_use is None


def test_record_liveness_changes_emits_unconsumed_queue_notification_paused_status(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """The distress fire carries the discriminating paused_status and names
    the notification text in its breadcrumb (#2251)."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _write_unconsumed_queue_transcript(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.PLAN)

    record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={"T-1": task},
    )

    events = _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(events) == 1
    assert events[0]["paused_status"] == _UNCONSUMED_QUEUE_NOTIFICATION_REASON
    breadcrumbs = str(events[0]["breadcrumbs"])
    assert _BG_DONE_TEXT in breadcrumbs
    assert "queue-operation" in breadcrumbs


# ---------------------------------------------------------------------------
# #2135: signal-only suppression for a row parked by the abandoned-exit park
# ---------------------------------------------------------------------------


def _parked_task(session_id: str | None, **overrides: object) -> TicketTask:
    """A row parked BLOCKED_ON_USER by the #2135 abandoned-exit park."""
    kwargs: dict[str, object] = {
        "ticket_id": "T-1",
        "client": "client-a",
        "stage": Stage.PLAN,
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": _STOPPED_WITHOUT_SENTINEL_REASON,
        "session_id": session_id,
    }
    kwargs.update(overrides)
    return TicketTask.model_validate(kwargs)


def _assert_top_bucket_latched(sess: object) -> None:
    """The bucket latch and its event are identical parked or not (#2135)."""
    assert sess.liveness_bucket == LivenessBucket.STALE_45M
    latches = _events_of(OrchestratorEventType.SESSION_LIVENESS_CHANGED)
    assert len(latches) == 1
    assert latches[0]["old_bucket"] == "live"
    assert latches[0]["new_bucket"] == "stale_45m"


def test_parked_stopped_without_sentinel_row_suppresses_session_unresponsive(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """The row already paged via its own session.needs_attention (#2135).

    Signal-only: the distress flag is withheld, nothing is mutated beyond the
    ordinary bucket latch, and no push fires.
    """
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={"T-1": _parked_task(sess.id)},
    )

    assert len(candidates) == 1
    assert candidates[0].new_bucket == LivenessBucket.STALE_45M
    assert candidates[0].distress is False
    assert candidates[0].next_renotify_eligible_at is None
    assert _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []
    _deps.fire_push_notification.assert_not_called()
    _assert_top_bucket_latched(sess)
    assert sess.liveness_attention_next_eligible_at is None


@pytest.mark.parametrize(
    "task_for",
    [
        lambda sid: _parked_task(sid, disposition="gh_check_blocked"),
        lambda sid: _parked_task(sid, disposition=None),
        lambda _sid: None,
        lambda sid: _parked_task(sid, status=QueueItemStatus.PENDING),
        lambda _sid: _parked_task("some-other-session"),
        lambda _sid: _parked_task(None),
    ],
    ids=[
        "other-disposition",
        "no-disposition",
        "no-task",
        "requeued-row",
        "other-session-row",
        "row-without-session-id",
    ],
)
def test_non_suppressed_task_shapes_still_emit_session_unresponsive(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    task_for: object,
) -> None:
    """Every shape but the exact parked triple keeps paging (fails open)."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])
    task = task_for(sess.id)

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={"T-1": task} if task is not None else {},
    )

    assert len(candidates) == 1
    assert candidates[0].distress is True
    attention = _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(attention) == 1
    assert attention[0]["paused_status"] == _SESSION_UNRESPONSIVE_REASON
    _deps.fire_push_notification.assert_called_once()
    _assert_top_bucket_latched(sess)


def test_suppressed_session_steady_state_and_recovery_latch_unchanged(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A suppressed crossing still latches, and a later transition still records."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])
    task_by_ticket = {"T-1": _parked_task(sess.id)}

    record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket=task_by_ticket,
    )

    steady = record_session_liveness_changes(
        state,
        now=_NOW + timedelta(minutes=1),
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket=task_by_ticket,
    )
    assert steady == []

    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=1)
    recovered = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket=task_by_ticket,
    )

    assert len(recovered) == 1
    assert recovered[0].new_bucket == LivenessBucket.LIVE
    assert sess.liveness_bucket == LivenessBucket.LIVE
    latches = _events_of(OrchestratorEventType.SESSION_LIVENESS_CHANGED)
    assert len(latches) == 2
    assert latches[1]["old_bucket"] == "stale_45m"
    assert latches[1]["new_bucket"] == "live"
    assert _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []


def _acting_task(act_session_id: str) -> TicketTask:
    """A RUNNING row carrying a mid-turn usage-limit act for *act_session_id*."""
    return TicketTask(
        ticket_id="T-1",
        client="client-a",
        stage=Stage.PLAN,
        status=QueueItemStatus.RUNNING,
        session_id=act_session_id,
        usage_limit_act=UsageLimitAct(
            session_id=act_session_id,
            branch="auto",
            started_at=_NOW,
            reset_at=None,
            until=_NOW + timedelta(minutes=30),
        ),
    )


@pytest.mark.parametrize(
    ("act_for_this_session", "expect_distress"),
    [
        pytest.param(True, False, id="act-for-this-session"),
        pytest.param(False, True, id="act-for-another-session"),
    ],
)
def test_usage_limit_act_in_flight_suppresses_session_unresponsive(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    act_for_this_session: bool,
    expect_distress: bool,
) -> None:
    """The mid-turn usage-limit act already paged and owns the row (#2324).

    Signal-only, like the #2135 carve-out: the bucket still latches, only the
    distress page is withheld -- and only for the act's own session.
    """
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])
    task = _acting_task(sess.id if act_for_this_session else "some-other-session")

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={"T-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].distress is expect_distress
    attention = _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(attention) == (1 if expect_distress else 0)
    _assert_top_bucket_latched(sess)


@pytest.mark.parametrize(
    ("park_for_this_session", "expect_distress"),
    [
        pytest.param(True, False, id="parked-for-this-session"),
        pytest.param(False, True, id="parked-for-another-session"),
    ],
)
def test_usage_limit_mid_turn_park_suppresses_session_unresponsive(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    park_for_this_session: bool,
    expect_distress: bool,
) -> None:
    """A signal-only park stays quiet after its transition clears the intent.

    The park transition clears ``usage_limit_act``, but leaves the session
    alive, the row BLOCKED_ON_USER / ``usage_limited_mid_turn`` and its
    ``session_id`` set -- already explained by the act's own page (#2324).
    Only the parked session is withheld; another session's row fails open.
    """
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])
    task = _parked_task(
        sess.id if park_for_this_session else "some-other-session",
        disposition=_USAGE_LIMITED_MID_TURN_REASON,
    )
    assert task.usage_limit_act is None

    candidates = record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={"T-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].distress is expect_distress
    attention = _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(attention) == (1 if expect_distress else 0)
    _assert_top_bucket_latched(sess)


def test_requeue_after_suppressed_crossing_pages_on_next_tick(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """The predicate is evaluated per tick: a requeued row pages again (#2135)."""
    sess, worktree = _mk_liveness_session(tmp_path=tmp_path)
    _stamp_transcript_stale_minutes(home, worktree, stale_minutes=60)
    state = CwState(sessions=[sess])

    record_session_liveness_changes(
        state,
        now=_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={"T-1": _parked_task(sess.id)},
    )
    assert _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []

    # requeue_ticket leaves the row PENDING; the disposition literal survives.
    requeued = _parked_task(sess.id, status=QueueItemStatus.PENDING)
    candidates = record_session_liveness_changes(
        state,
        now=_NOW + timedelta(minutes=1),
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={"T-1": requeued},
    )

    assert len(candidates) == 1
    assert candidates[0].distress is True
    attention = _events_of(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(attention) == 1
    assert attention[0]["paused_status"] == _SESSION_UNRESPONSIVE_REASON
    _deps.fire_push_notification.assert_called_once()
