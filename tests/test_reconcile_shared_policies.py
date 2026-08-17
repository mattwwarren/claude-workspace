"""Unit tests for cw.reconcile._shared — reap-policy routing and sentinels.

Reap-proposed emission, per-lane reap_policy resolution, emitted-sentinel
routing, and _apply_sentinel_to_task disposition.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cw.auto_dev_result import (
    BLOCKER_REASON_VALIDATION_FAILED,
    AutoDevResult,
    BlockedResult,
    Blocker,
    parse_stdout,
)
from cw.config import (
    load_state,
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
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import (
    _VALIDATION_FAILED_MAX_ATTEMPTS,
    SentinelRouteOutcome,
    _act_on_idle_candidates,
    _apply_sentinel_to_task,
    _detect_idle_candidates,
    _has_terminal_sentinel,
)
from cw.reconcile._shared import (
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    _route_blocked_result_to_task,
)
from tests._reconcile_helpers import (
    _auto_config,
    _client_with_lane,
    _make_terminal_payload,
    _mk_headless_daemon_session,
    _mk_session,
    _no_op_salvage_payload,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _state_queue_snapshot,
    _write_salvage_transcript,
    _write_staged_clients_yaml,
    _write_transcript_records,
)
from tests.conftest import _make_daemon_session


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

        # No transcript file was written for this session — both age fields
        # must fail open to None rather than raising or defaulting to 0 (#1427).
        evidence = ev.payload["evidence"]
        assert evidence["transcript_age_seconds"] is None
        assert evidence["transcript_mtime_age_seconds"] is None

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

    def test_reap_proposed_stalled_retry_cap_carries_correction_signal_fields(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """STALLED_RETRY_CAP_PARKED reason → payload carries crashed/
        regress_attempts/spawn_error_count (#1625)."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("prop-cap-1", "live-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.started_at = started_at
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-cap-1",
            proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
            ticket_id="prop-cap-1",
            reap_reason=ReapReason.STALLED_RETRY_CAP_PARKED,
            regress_attempts=0,
            spawn_error_count=0,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        payload = reap_events[0].payload
        assert payload["reason"] == "stalled_retry_cap_parked"
        assert payload["crashed"] is False
        assert payload["regress_attempts"] == 0
        assert payload["spawn_error_count"] == 0

    def test_reap_proposed_non_cap_reason_omits_correction_signal_fields(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Non-cap reap_reason → payload does NOT carry crashed/regress_attempts/
        spawn_error_count (negative guard, #1625)."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("prop-noncap-1", "live-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.started_at = started_at
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-noncap-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-noncap-1",
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
            regress_attempts=0,
            spawn_error_count=0,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        payload = reap_events[0].payload
        assert payload["reason"] == "wall_clock_budget"
        assert "crashed" not in payload
        assert "regress_attempts" not in payload
        assert "spawn_error_count" not in payload

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

    def test_reap_proposed_evidence_uses_content_age_not_mtime(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """evidence.transcript_age_seconds reflects content-entry staleness, not
        raw mtime, when a trailing metadata-only record (queue-operation/
        ai-title/mode — no timestamp, no message) bumped the file's mtime after
        the last real turn (#1427).
        """
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        worktree = tmp_path / "wt-prop-content-age"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)  # 2h after start
        old_content_ts = now - timedelta(hours=1, minutes=46)  # ~106min old

        transcript = _write_transcript_records(
            home,
            worktree,
            [
                {
                    "type": "user",
                    "timestamp": old_content_ts.isoformat(),
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "go"}],
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": old_content_ts.isoformat(),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "working"}],
                    },
                },
                # Trailing metadata-only records: no "timestamp", no "message" —
                # these bump the file's mtime without representing live activity.
                {"type": "queue-operation", "op": "dequeue"},
                {"type": "last-prompt", "prompt": "go"},
                {"type": "ai-title", "title": "Fix the thing"},
                {"type": "agent-name", "name": "worker"},
                {"type": "mode", "mode": "default"},
                {"type": "permission-mode", "mode": "acceptEdits"},
            ],
        )
        # Bump mtime to just before `now` — ~221s before, mirrors a fresh-mtime
        # trailing metadata write.
        recent_mtime = (now - timedelta(seconds=221)).timestamp()
        os.utime(transcript, (recent_mtime, recent_mtime))

        sess = _mk_headless_daemon_session("prop-content-age-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-content-age-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-content-age-1",
            elapsed_seconds=7436.79,
            reap_reason=ReapReason.STALLED_RETRY_CAP_PARKED,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        evidence = reap_events[0].payload["evidence"]

        # Content-aware age: ~106 minutes (6360s), NOT the ~221s mtime age.
        content_age = evidence["transcript_age_seconds"]
        assert content_age is not None
        expected_content_age = (now - old_content_ts).total_seconds()
        assert abs(content_age - expected_content_age) < 5

        # The raw mtime age is still reported, but under the distinct field name.
        mtime_age = evidence["transcript_mtime_age_seconds"]
        assert mtime_age is not None
        assert abs(mtime_age - 221) < 5

        # The two must diverge by orders of magnitude in this fixture, matching
        # the bug report (this assertion documents *why* two fields exist).
        assert content_age > mtime_age * 10

    def test_reap_proposed_evidence_ages_equal_without_trailing_metadata(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When a transcript has only content-bearing entries (mtime == last
        content timestamp), transcript_age_seconds and
        transcript_mtime_age_seconds must agree — guards against an off-by-`now`
        construction bug in the new field (#1427)."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        worktree = tmp_path / "wt-prop-no-divergence"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        content_ts = now - timedelta(seconds=300)

        transcript = _write_transcript_records(
            home,
            worktree,
            [
                {
                    "type": "user",
                    "timestamp": content_ts.isoformat(),
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "go"}],
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": content_ts.isoformat(),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    },
                },
            ],
        )
        mtime = content_ts.timestamp()
        os.utime(transcript, (mtime, mtime))

        sess = _mk_headless_daemon_session("prop-no-divergence-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-no-divergence-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-no-divergence-1",
            elapsed_seconds=300.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        evidence = reap_events[0].payload["evidence"]

        content_age = evidence["transcript_age_seconds"]
        mtime_age = evidence["transcript_mtime_age_seconds"]
        assert content_age is not None
        assert mtime_age is not None
        assert abs(content_age - mtime_age) < 1


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


def _run_emitted_sentinel_router(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> None:
    """Drive the emitted-sentinel router's detect + act phases in one call.

    Test-local stand-in for the removed ``flag_silently_idle_daemon_sessions``
    driver: since the process-kill-timeout removal the idle sweep consists of
    exactly these two phases, so detect + act IS the whole sweep.
    """
    resolved = (
        task_by_ticket
        if task_by_ticket is not None
        else {t.ticket_id: t for t in load_dev_queue().tasks}
    )
    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live=native_live,
        config=config,
        task_by_ticket=resolved,
    )
    _act_on_idle_candidates(state, candidates, now=now)


class TestRouteEmittedSentinel:
    """The emitted-sentinel router routes a transcript sentinel that
    signal_stop never consumed (GitHub #578)."""

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
        # 305 s elapsed — past the 300-s unrouted-sentinel check delay.
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)
        assert (now - started_at).total_seconds() >= 300

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
            _run_emitted_sentinel_router(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

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
        _run_emitted_sentinel_router(
            state,
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),
        )

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
        _run_emitted_sentinel_router(
            state,
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),
        )

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
            _run_emitted_sentinel_router(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

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
            _run_emitted_sentinel_router(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

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
            _run_emitted_sentinel_router(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY),
            )

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
            _run_emitted_sentinel_router(
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
            _run_emitted_sentinel_router(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

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
            _run_emitted_sentinel_router(
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
            _run_emitted_sentinel_router(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
                task_by_ticket={"1149-idle-later": task},
            )

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
        session = _make_daemon_session(id=session_id, worktree_path=None)

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
        _apply_sentinel_to_task(ticket_id, session, sentinel)

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        _apply_sentinel_to_task(ticket_id, session, sentinel)

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
        session = _make_daemon_session(id=session_id, worktree_path=None)

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
        _apply_sentinel_to_task(ticket_id, session, sentinel)

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
        session = _make_daemon_session(id=session_id, worktree_path=None)

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
        _apply_sentinel_to_task(ticket_id, session, sentinel)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_apply_sentinel_to_task_earlier_stage_blocked_now_routes(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """GitHub #1676: an earlier-stage 'blocked' sentinel (non-advance-claim)
        now routes end-to-end through _apply_sentinel_to_task instead of being
        refused as an #1019/#986 stage-mismatch replay.

        Task sits at IMPL; the sentinel reports 'blocked' at stage1_plan
        (PLAN, earlier than IMPL) -- a legitimate "could not reach the
        dispatched stage" report, not a same-session replay, since 'blocked'
        is not in STAGE_SUCCESS_STATUSES.
        """
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)

        ticket_id = "GH-1676-earlier-blocked"
        session_id = "sess-1676-earlier-blocked"
        session = _make_daemon_session(id=session_id, worktree_path=None)

        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = AutoDevResult.model_validate(
            _blocked_autodev_payload(
                ticket_id,
                stage_reached="stage1_plan",
                scope={
                    "tier": None,
                    "files": 4,
                    "lines_estimate": 120,
                    "lines_actual": None,
                    "forbidden_touched": False,
                },
                blocker={"stage": "s1_plan", "reason": "plan_missing"},
            )
        )

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

        assert outcome.routed is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.blocked_reason == "plan_missing"


def _blocked_autodev_payload(
    ticket_id: str,
    *,
    stage_reached: str = "stage2_impl",
    scope: dict[str, Any] | None = None,
    blocker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Minimal valid AutoDevResult with status='blocked'.

    Routes through STAGE_FAILURE (Rule 5) → BLOCKED_ON_USER when applied.
    Defaults reproduce the original post-impl (stage2_impl) shape; pass
    ``stage_reached``/``scope``/``blocker`` to reshape for other stages -- e.g.
    #1676's pre-impl (stage1_plan) earlier-stage-blocked coverage, which needs
    ``scope.lines_actual=None`` to satisfy the pre-impl-exit invariant.
    """
    return {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "status": "blocked",
        "stage_reached": stage_reached,
        "scope": scope
        or {
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
        "blocker": blocker
        or {
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
        session = _make_daemon_session(id=session_id, worktree_path=None)
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_late_shipped_rescues_parked_task_terminal_preserves_pr_url(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked at FINALIZE + late shipped → COMPLETED + pr_url; return True."""
        ticket_id, session_id = "GH-918-ship", "sess-918-ship"
        session = _make_daemon_session(id=session_id, worktree_path=None)
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.FINALIZE,
        )
        sentinel = AutoDevResult.model_validate(_shipped_salvage_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED
        assert t.disposition == "shipped"
        assert t.pr_url == "https://github.com/foo/bar/pull/99"

    def test_late_no_op_rescues_parked_task(self, tmp_config_dir: Path) -> None:
        """Parked + late no_op → COMPLETED disposition='no_op'; return True."""
        ticket_id, session_id = "GH-918-noop", "sess-918-noop"
        session = _make_daemon_session(id=session_id, worktree_path=None)
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.PLAN,
        )
        sentinel = AutoDevResult.model_validate(_no_op_salvage_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session, sentinel).rescued

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        sentinel = AutoDevResult.model_validate(_blocked_autodev_payload(ticket_id))

        rescued = _apply_sentinel_to_task(ticket_id, session, sentinel).rescued

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        rescued = _apply_sentinel_to_task(ticket_id, session, sentinel).rescued

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        rescued = _apply_sentinel_to_task(ticket_id, session, sentinel).rescued

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session, sentinel).rescued

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        rescued = _apply_sentinel_to_task(ticket_id, session, sentinel).rescued

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.REVIEW,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

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
            "GH-1019-no-target",
            _make_daemon_session(id="sess-no-target", worktree_path=None),
            sentinel,
        )

        assert outcome == SentinelRouteOutcome(
            rescued=False, routed=True, landed_terminal=False
        )


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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

        assert outcome.routed is False
        assert outcome.rescued is False
        assert outcome.landed_terminal is True
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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

        assert outcome.routed is False
        assert outcome.landed_terminal is True
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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

        assert outcome.routed is False
        assert outcome.landed_terminal is True
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

        #1406: doubles as the UNKNOWN-liveness regression pin --
        ``worktree_path=None`` makes ``_transcript_age_seconds`` return None
        (no project dir to glob), so the catch-all's new liveness veto cannot
        fire and this FAILED landing must be preserved exactly.
        """
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

        session = _make_daemon_session(id="sess-1266-catchall", worktree_path=None)

        routed = _route_blocked_result_to_task(target, session, sentinel)

        assert routed is False
        assert target.status == QueueItemStatus.FAILED
        assert target.disposition == "abandoned"
        assert target.last_blocked_result == sentinel.model_dump(mode="json")
        assert [
            e
            for e in read_events()
            if e.type == OrchestratorEventType.SESSION_SENTINEL_LIVENESS_VETOED
        ] == []

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

        assert outcome == SentinelRouteOutcome(
            rescued=False, routed=False, landed_terminal=False
        )
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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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
            outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

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
            "GH-1189-no-match",
            _make_daemon_session(id="sess-1189-no-match", worktree_path=None),
            sentinel,
        )

        assert outcome == SentinelRouteOutcome(
            rescued=False, routed=True, landed_terminal=False
        )

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

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
        session = _make_daemon_session(id=session_id, worktree_path=None)
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

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

        assert outcome == SentinelRouteOutcome(
            rescued=False, routed=True, landed_terminal=False
        )


class TestRouteBlockedResultCatchAllLivenessGuard:
    """GitHub #1406: the catch-all FAILED/abandoned landing must not fire while
    the owning session's transcript is still actively advancing.

    Sibling closure to #1281, which added the same transcript-liveness veto to
    the phantom sweep's stage-mismatch fall-through. #1406 closes the
    alternative incident route that survived it: a RUNNING task whose
    BlockedResult is merely *unparseable* (status_unknown,
    multiple_result_blocks, any unrecognized reason) landed terminal-FAILED
    even when the worker behind it was still making progress. LIVE now
    re-queues to PENDING; DEAD (age past the window) and UNKNOWN (no locatable
    transcript) both fall through to the pre-existing FAILED landing.
    """

    CATCH_ALL_REASON = "unknown_reason_xyz"

    def _live_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        session_id: str,
        age_seconds: float,
    ) -> tuple[Session, datetime]:
        """Build a DAEMON session whose transcript is ``age_seconds`` old at now.

        Returns ``(session, now)``.  ``now`` is a fixed offset past the
        session's ``started_at`` so the transcript's ``os.utime``-stamped mtime
        stays strictly after it -- ``_project_transcripts_latest_timestamp``'s
        reused-worktree guard (#358/#372) discards any candidate older than
        ``started_at``.
        """
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / f"wt-{session_id}"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(hours=1)

        transcript = _write_salvage_transcript(
            home, worktree, f"csid-{session_id}", _stage_complete_payload()
        )
        mtime = (now - timedelta(seconds=age_seconds)).timestamp()
        os.utime(str(transcript), (mtime, mtime))

        session = _make_daemon_session(
            id=session_id,
            worktree_path=worktree,
            surface_ref="fake-short-id",
            started_at=started_at,
        )
        return session, now

    def _catch_all_sentinel(self) -> BlockedResult:
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=self.CATCH_ALL_REASON,
                details="test: unrecognised reason code",
            )
        )

    def test_route_blocked_result_catch_all_live_transcript_requeues_pending(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LIVE transcript → PENDING + cleared session_id instead of FAILED.

        Mirrors the ``_TRANSIENT_PARSE_FAILURES`` branch's requeue, but
        (unlike that branch) also emits ``SESSION_SENTINEL_LIVENESS_VETOED``
        -- this veto overrides what would otherwise be a terminal FAILED
        landing, so it needs a durable audit trail; a re-queue that never
        would have been FAILED doesn't. Does NOT write the
        ``last_blocked_result`` diagnostic (#1266): the veto is a re-queue,
        not a rejection, so there is no rejected sentinel to record.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        session, now = self._live_session(
            tmp_path, monkeypatch, session_id="sess-1406-live", age_seconds=30
        )
        target = TicketTask(
            ticket_id="GH-1406-live",
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session.id,
            stage=Stage.IMPL,
            attempts=1,
        )
        sentinel = self._catch_all_sentinel()

        routed = _route_blocked_result_to_task(target, session, sentinel, now=now)

        assert routed is True
        assert target.status == QueueItemStatus.PENDING
        assert target.session_id is None
        assert target.last_blocked_result is None
        vetoed = [
            e
            for e in read_events()
            if e.type == OrchestratorEventType.SESSION_SENTINEL_LIVENESS_VETOED
        ]
        assert len(vetoed) == 1
        assert vetoed[0].payload["ticket_id"] == "GH-1406-live"
        assert vetoed[0].payload["client"] == "staged-client"
        assert vetoed[0].payload["session_id"] == session.id
        # Tolerance, not exact equality: transcript_age_seconds round-trips
        # through a real os.utime write + disk stat() read-back (mtime
        # fallback, since the salvage payload carries no content timestamp),
        # matching the convention test_reconcile_shared_sentinels.py already
        # uses for the same underlying value.
        assert abs(vetoed[0].payload["transcript_age_seconds"] - 30) < 5
        assert vetoed[0].payload["blocker_reason"] == self.CATCH_ALL_REASON

    def test_route_blocked_result_catch_all_stale_transcript_falls_through_to_failed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DEAD transcript (age past the window) → the pre-#1406 FAILED landing.

        Distinct from the UNKNOWN case pinned by
        ``test_route_blocked_result_catch_all_writes_last_blocked_result``: here
        the age is *known*, just past ``TRANSCRIPT_LIVENESS_WINDOW_SECONDS``.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        session, now = self._live_session(
            tmp_path,
            monkeypatch,
            session_id="sess-1406-stale",
            age_seconds=TRANSCRIPT_LIVENESS_WINDOW_SECONDS + 60,
        )
        target = TicketTask(
            ticket_id="GH-1406-stale",
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session.id,
            stage=Stage.IMPL,
            attempts=1,
        )
        sentinel = self._catch_all_sentinel()

        routed = _route_blocked_result_to_task(target, session, sentinel, now=now)

        assert routed is False
        assert target.status == QueueItemStatus.FAILED
        assert target.disposition == "abandoned"
        assert target.last_blocked_result == sentinel.model_dump(mode="json")
        assert [
            e
            for e in read_events()
            if e.type == OrchestratorEventType.SESSION_SENTINEL_LIVENESS_VETOED
        ] == []

    def test_apply_sentinel_catch_all_live_transcript_requeues_pending_via_apply(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The veto reaches production through ``_apply_sentinel_to_task``.

        Pins the widened ``session``/``now`` threading end-to-end, and that
        ``landed_terminal`` (#1273) derives False off the vetoed ``routed=True``
        -- callers must NOT ``daemon.stop()`` a worker that is still advancing.
        Also confirms the audit event survives the trip through
        ``_apply_sentinel_to_task``'s own ``dev_queue_lock()`` -- record_event
        nests _inbox_lock inside it, the established safe order (RFC 0008 W1,
        #978 -- not #765, which is the opposite lesson: a deadlock from the
        reverse mistake in a since-fixed call site).
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        session, now = self._live_session(
            tmp_path, monkeypatch, session_id="sess-1406-apply", age_seconds=30
        )
        ticket_id = "GH-1406-apply"
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="staged-client",
                        status=QueueItemStatus.RUNNING,
                        session_id=session.id,
                        stage=Stage.IMPL,
                        attempts=1,
                    )
                ]
            )
        )
        sentinel = self._catch_all_sentinel()

        outcome = _apply_sentinel_to_task(ticket_id, session, sentinel, now=now)

        assert outcome.routed is True
        assert outcome.landed_terminal is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None
        vetoed = [
            e
            for e in read_events()
            if e.type == OrchestratorEventType.SESSION_SENTINEL_LIVENESS_VETOED
        ]
        assert len(vetoed) == 1
        assert vetoed[0].payload["ticket_id"] == ticket_id

    def test_route_blocked_result_catch_all_negative_age_falls_through_to_failed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``0 <= age`` floor: a transcript mtime *after* ``now`` is not LIVE.

        A negative age (clock skew, or a caller-supplied fictional/frozen
        ``now`` that predates a transcript's real wall-clock mtime) must not
        be misclassified as freshly-live -- it falls through to the same
        FAILED landing as DEAD/UNKNOWN. Pins the floor directly, rather than
        relying on it as an incidental side effect of two unrelated tests
        (``test_reconcile_idle.py`` and ``test_cli.py``'s ``TestSignalStop``)
        that happen to combine a fictional/frozen ``now`` with a real
        transcript mtime, neither of which names this invariant.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        session, now = self._live_session(
            tmp_path, monkeypatch, session_id="sess-1406-negative-age", age_seconds=-30
        )
        target = TicketTask(
            ticket_id="GH-1406-negative-age",
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session.id,
            stage=Stage.IMPL,
            attempts=1,
        )
        sentinel = self._catch_all_sentinel()

        routed = _route_blocked_result_to_task(target, session, sentinel, now=now)

        assert routed is False
        assert target.status == QueueItemStatus.FAILED
        assert target.disposition == "abandoned"
        assert target.last_blocked_result == sentinel.model_dump(mode="json")
        assert [
            e
            for e in read_events()
            if e.type == OrchestratorEventType.SESSION_SENTINEL_LIVENESS_VETOED
        ] == []

    @pytest.mark.parametrize(
        ("age_seconds", "expect_routed", "expect_status", "expect_event_count"),
        [
            pytest.param(
                TRANSCRIPT_LIVENESS_WINDOW_SECONDS - 1,
                True,
                QueueItemStatus.PENDING,
                1,
                id="just_inside_window",
            ),
            pytest.param(
                TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
                False,
                QueueItemStatus.FAILED,
                0,
                id="exactly_at_window",
            ),
        ],
    )
    def test_route_blocked_result_catch_all_window_boundary(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        age_seconds: int,
        expect_routed: bool,
        expect_status: QueueItemStatus,
        expect_event_count: int,
    ) -> None:
        """The window comparison is strict ``<``: exactly at the window is DEAD.

        ``TRANSCRIPT_LIVENESS_WINDOW_SECONDS - 1`` (just inside) requeues to
        PENDING and emits the veto event; ``TRANSCRIPT_LIVENESS_WINDOW_SECONDS``
        exactly (not strictly less than the window) falls through to FAILED
        with no event.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        session, now = self._live_session(
            tmp_path,
            monkeypatch,
            session_id="sess-1406-boundary",
            age_seconds=age_seconds,
        )
        target = TicketTask(
            ticket_id="GH-1406-boundary",
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session.id,
            stage=Stage.IMPL,
            attempts=1,
        )
        sentinel = self._catch_all_sentinel()

        routed = _route_blocked_result_to_task(target, session, sentinel, now=now)

        assert routed is expect_routed
        assert target.status == expect_status
        vetoed = [
            e
            for e in read_events()
            if e.type == OrchestratorEventType.SESSION_SENTINEL_LIVENESS_VETOED
        ]
        assert len(vetoed) == expect_event_count
        if expect_status == QueueItemStatus.PENDING:
            assert target.session_id is None
        else:
            assert target.disposition == "abandoned"
            assert target.last_blocked_result == sentinel.model_dump(mode="json")


def test_validation_failed_cap_still_reads_raw_attempts_not_unproductive(
    tmp_config_dir: Path,
) -> None:
    """#1750 non-interference: the #756 per-stage cap did NOT move counters.

    A worker that keeps emitting an unparseable sentinel may have committed
    real work each time, so it can read as fully productive. If this cap had
    been moved to unproductive_attempts alongside the global ceiling, that
    worker would retry forever. It must still trip off raw ``attempts``.
    """
    _write_staged_clients_yaml(tmp_config_dir, "staged-client")
    ticket_id, session_id = "GH-1750-vfcap", "sess-1750-vfcap"
    session = _make_daemon_session(id=session_id, worktree_path=None)
    task = TicketTask(
        ticket_id=ticket_id,
        client="staged-client",
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
        stage=Stage.IMPL,
        attempts=_VALIDATION_FAILED_MAX_ATTEMPTS,
        unproductive_attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    sentinel = BlockedResult(
        blocker=Blocker(
            stage="unknown",
            reason=BLOCKER_REASON_VALIDATION_FAILED,
            details="test: validation failed at cap, zero unproductive attempts",
        )
    )

    outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

    assert outcome.routed is False
    t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert t.status == QueueItemStatus.FAILED
    assert t.disposition == "abandoned"
