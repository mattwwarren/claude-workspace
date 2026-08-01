"""Unit tests for cw.reconcile.salvage.

Git-state salvage post-pass: committed-no-PR draft/flag disposition,
finalize-blocked rescue, and salvage reap-reason stamping.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
    OrchestratorEventType,
    QueueItemStatus,
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
    _DIRTY_WORKTREE_REASON,
    _NEEDS_SALVAGE_REASON,
    _SALVAGE_KIND_GIT_STATE,
    _STAGE_REVIEW_COMPLETE,
    UsageLimitDetection,
    flag_silently_idle_daemon_sessions,
    reconcile,
    revert_stalled_headless_sessions,
    revert_timed_out_tasks,
    salvage_committed_no_pr_sessions,
)
from tests._reconcile_helpers import (
    _auto_config,
    _mk_timed_out_daemon_session,
    _ul_record,
    _write_staged_clients_yaml,
    _write_transcript_records,
)
from tests.conftest import _make_daemon_session, _make_ticket_task

# #1345: the salvage low-path gates detect_usage_limit through
# usage_limit_is_recent(..., fail_open=False, 60s). A recent detection (match at
# the transcript tail) survives that gate as True; a not-detected result is False.
_UL_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_RECENT_UL = UsageLimitDetection(
    detected=True, matched_at=_UL_NOW, transcript_tail_at=_UL_NOW
)
_NO_UL = UsageLimitDetection(detected=False, matched_at=None, transcript_tail_at=None)


def _mk_live_daemon_session_with_worktree(
    sid: str,
    worktree: Path,
    ticket_id: str,
) -> Session:
    """Build a live DAEMON ACTIVE session with a headless context and worktree."""
    sess = _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{ticket_id}",
        worktree_path=worktree,
    )
    context_dir = worktree / ".claude"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "cw-context.json").write_text(
        '{"headless": true, "session_id": "' + sid + '"}'
    )
    return sess


def _write_stage_event(
    session_id: str,
    stage: str,
    started_at: datetime,
    *,
    offset_seconds: int = 60,
) -> None:
    """Write a STAGE_ENTERED event for testing _detect_post_review_clean."""
    from cw.events import record_event
    from cw.models import OrchestratorEventType

    record_event(
        OrchestratorEventType.STAGE_ENTERED,
        {
            "session_id": session_id,
            "stage": stage,
        },
    )


class TestSalvageCommittedNoPrSessions:
    """Tests for salvage_committed_no_pr_sessions (GitHub issue #497)."""

    def test_high_path_creates_draft_pr_and_completes_task(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HIGH path: post-review clean + commits + no PR → draft PR created,
        task COMPLETED, SESSION_COMPLETED with salvage_kind=git_state_salvage."""
        worktree = tmp_path / "wt-high"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-HIGH"
        sess = _mk_live_daemon_session_with_worktree("sess-high", worktree, ticket_id)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-high",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # Write a stage event for post-review clean
        _write_stage_event("sess-high", _STAGE_REVIEW_COMPLETE, sess.started_at)

        push_calls: list[object] = []
        gh_calls: list[object] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            result = MagicMock()
            result.returncode = 0
            if args[:2] == ["git", "push"]:
                push_calls.append(args)
                result.stdout = ""
                return result
            if args[:2] == ["gh", "pr"]:
                gh_calls.append(args)
                result.stdout = "https://github.com/org/repo/pull/42\n"
                return result
            return result

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        # First call (pre-check): no PR; second call (idempotency): no PR.
        # Capture cwd from both to prove it is scoped to client-a's repo (#1279).
        pr_cwds: list[object] = []

        def _capture_pr_exists(_b: str, **kw: object) -> tuple[bool | None, bool]:
            pr_cwds.append(kw.get("cwd"))
            return False, True

        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", _capture_pr_exists
        )
        monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_subprocess_run)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client",
            MagicMock,
        )

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-high", ticket_id, "dev/high-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert ticket_id in completed
        # Both the pre-check and the _salvage_high_path recheck are scoped.
        assert pr_cwds == [Path("/tmp/ws-staged"), Path("/tmp/ws-staged")]

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-high-path-salvage",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        salvage_events = [
            e
            for e in events
            if e.payload.get("salvage_kind") == _SALVAGE_KIND_GIT_STATE
        ]
        assert len(salvage_events) == 1
        assert salvage_events[0].payload.get("draft") is True
        assert "github.com" in str(salvage_events[0].payload.get("pr", ""))

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-high")
        assert s.status == SessionStatus.COMPLETED

    def test_low_path_flags_needs_salvage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW path: commits + no PR + no post-review clean → needs_salvage,
        task BLOCKED_ON_USER, SESSION_NEEDS_ATTENTION with breadcrumbs."""
        worktree = tmp_path / "wt-low"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-LOW"
        sess = _mk_live_daemon_session_with_worktree("sess-low", worktree, ticket_id)
        sess.lane = "salvage-lane"
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-low",
                    )
                ]
            )
        )
        # No stage event written → post_review_clean=False

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-low", ticket_id, "dev/low-branch", str(worktree), False)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

        events = read_events(
            consumer="test-low-path-salvage",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert len(attn_events) == 1
        bc = attn_events[0].payload.get("breadcrumbs", "")
        assert "dev/low-branch" in str(bc)
        assert str(worktree) in str(bc)
        assert attn_events[0].payload["lane"] == "salvage-lane"

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-low")
        lr = s.last_result or {}
        assert lr.get("paused_status") == _NEEDS_SALVAGE_REASON
        assert s.status == SessionStatus.COMPLETED

    def test_low_path_flags_needs_salvage_lane_none_survives_uncoerced(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW path with session.lane=None: payload carries lane=None, not
        coerced to DEFAULT_LANE or any other default (#1333 R4)."""
        worktree = tmp_path / "wt-low-none-lane"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-LOW-NONE-LANE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-low-none-lane", worktree, ticket_id
        )
        assert sess.lane is None  # precondition: default, not stamped
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-low-none-lane",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            (
                "sess-low-none-lane",
                ticket_id,
                "dev/low-none-lane-branch",
                str(worktree),
                False,
            )
        ]
        salvage_committed_no_pr_sessions(candidates)

        events = read_events(
            consumer="test-low-path-salvage-none-lane",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert len(attn_events) == 1
        assert attn_events[0].payload["lane"] is None

    def test_low_path_usage_limit_true_clean_worktree_routes_to_timed_out_cutoff(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW path + usage-limit cutoff detected → TIMED_OUT/USAGE_LIMIT_CUTOFF,
        NOT needs_salvage/CRASHED; task stays RUNNING (no BLOCKED_ON_USER); no
        SESSION_NEEDS_ATTENTION false alarm (#1336)."""
        worktree = tmp_path / "wt-ul-clean"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-UL-CLEAN"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-ul-clean", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ul-clean",
                    )
                ]
            )
        )
        # No stage event written → post_review_clean=False (LOW path)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.detect_usage_limit", lambda _s: _RECENT_UL
        )
        mock_daemon = MagicMock()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client",
            lambda: mock_daemon,
        )

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-ul-clean", ticket_id, "dev/ul-clean-branch", str(worktree), False)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-ul-clean")
        assert s.status == SessionStatus.TIMED_OUT
        assert s.completed_reason == CompletionReason.TIMED_OUT
        assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF
        lr = s.last_result or {}
        assert lr.get("paused_status") != _NEEDS_SALVAGE_REASON

        # Task must NOT be routed to BLOCKED_ON_USER — stays RUNNING for the
        # tasks.py:revert_timed_out_tasks backstop to pick up next tick.
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

        # No SESSION_NEEDS_ATTENTION false alarm for needs_salvage.
        events = read_events(
            consumer="test-ul-clean-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert attn_events == []

        # Daemon surface cleanup still happens on both branches.
        mock_daemon.stop.assert_called_once_with("live-ref")

    def test_low_path_usage_limit_false_keeps_needs_salvage_disposition(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit-mock companion to test_low_path_flags_needs_salvage: with
        detect_usage_limit patched to return False, behavior is unchanged from
        the pre-#1336 needs_salvage disposition."""
        worktree = tmp_path / "wt-ul-false"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-UL-FALSE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-ul-false", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ul-false",
                    )
                ]
            )
        )
        # No stage event written → post_review_clean=False

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.detect_usage_limit", lambda _s: _NO_UL
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-ul-false", ticket_id, "dev/ul-false-branch", str(worktree), False)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

        events = read_events(
            consumer="test-ul-false-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert len(attn_events) == 1

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-ul-false")
        lr = s.last_result or {}
        assert lr.get("paused_status") == _NEEDS_SALVAGE_REASON
        assert s.reap_reason == ReapReason.SALVAGE_PARKED
        assert s.status == SessionStatus.COMPLETED
        assert s.completed_reason == CompletionReason.CRASHED

    def _run_low_path_with_transcript(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        slug: str,
        records: list[dict[str, object]],
    ) -> Session:
        """Drive the salvage low-path against a REAL detector + transcript.

        Returns the reloaded session so callers can assert on its disposition.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        worktree = tmp_path / f"wt-{slug}"
        worktree.mkdir(parents=True)
        ticket_id = f"TKT-{slug.upper()}"
        sess = _mk_live_daemon_session_with_worktree(
            f"sess-{slug}", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id=f"sess-{slug}",
                    )
                ]
            )
        )
        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        transcript = _write_transcript_records(
            home, worktree, records, filename="live-ref-sess-1345.jsonl"
        )
        after_ts = sess.started_at.timestamp() + 60
        os.utime(str(transcript), (after_ts, after_ts))

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            (f"sess-{slug}", ticket_id, f"dev/{slug}-branch", str(worktree), False)
        ]
        salvage_committed_no_pr_sessions(candidates)

        reloaded = load_state()
        return next(s for s in reloaded.sessions if s.id == f"sess-{slug}")

    def test_low_path_stamps_usage_limit_cutoff_when_limit_message_at_tail(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1345: a real limit message at the transcript tail passes the tight
        60s fail-closed gate → USAGE_LIMIT_CUTOFF disposition."""
        s = self._run_low_path_with_transcript(
            tmp_path,
            tmp_config_dir,
            monkeypatch,
            slug="ul-tail",
            records=[
                _ul_record("committing work", "2026-01-01T00:00:10+00:00"),
                _ul_record(
                    "You've hit your session limit · resets 5am",
                    "2026-01-01T00:00:40+00:00",
                ),
            ],
        )
        assert s.status == SessionStatus.TIMED_OUT
        assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF
        lr = s.last_result or {}
        assert lr.get("paused_status") != _NEEDS_SALVAGE_REASON

    def test_low_path_parks_for_salvage_when_limit_message_stale(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1345 regression: an early limit message the worker recovered from,
        followed by a later crash, must NOT be mislabeled a live cutoff — the
        60s window fails and the needs_salvage disposition is preserved."""
        s = self._run_low_path_with_transcript(
            tmp_path,
            tmp_config_dir,
            monkeypatch,
            slug="ul-stale",
            records=[
                _ul_record(
                    "You've hit your session limit · resets 5am",
                    "2026-01-01T00:00:10+00:00",
                ),
                # 90s later — beyond the 60s salvage window.
                _ul_record("resumed and kept working", "2026-01-01T00:01:40+00:00"),
            ],
        )
        assert s.status == SessionStatus.COMPLETED
        assert s.completed_reason == CompletionReason.CRASHED
        assert s.reap_reason == ReapReason.SALVAGE_PARKED
        lr = s.last_result or {}
        assert lr.get("paused_status") == _NEEDS_SALVAGE_REASON

    def test_low_path_parks_for_salvage_when_timestamps_missing(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1345 fail-closed mandate: a detected limit message with NO parseable
        timestamp yields no anchor; with fail_open=False the salvage low-path
        must NOT stamp a cutoff — genuine behavior change vs the old detector."""
        s = self._run_low_path_with_transcript(
            tmp_path,
            tmp_config_dir,
            monkeypatch,
            slug="ul-nots",
            records=[
                _ul_record("You've hit your session limit · resets 5am"),
            ],
        )
        assert s.status == SessionStatus.COMPLETED
        assert s.completed_reason == CompletionReason.CRASHED
        assert s.reap_reason == ReapReason.SALVAGE_PARKED
        lr = s.last_result or {}
        assert lr.get("paused_status") == _NEEDS_SALVAGE_REASON

    def test_low_path_usage_limit_true_dirty_worktree_still_parks_via_backstop(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Usage-limit cutoff stamps TIMED_OUT/USAGE_LIMIT_CUTOFF, but a
        dirty worktree still routes the task to BLOCKED_ON_USER via the
        tasks.py:revert_timed_out_tasks backstop — the reap_reason stamped by
        salvage.py survives untouched (#1336)."""
        worktree = tmp_path / "wt-ul-dirty"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-UL-DIRTY"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-ul-dirty", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ul-dirty",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.detect_usage_limit", lambda _s: _RECENT_UL
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-ul-dirty", ticket_id, "dev/ul-dirty-branch", str(worktree), False)
        ]
        salvage_committed_no_pr_sessions(candidates)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-ul-dirty")
        assert s.status == SessionStatus.TIMED_OUT
        assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF

        # Now simulate the backstop tick observing a dirty worktree.
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: "dev/ul-dirty-branch",
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.get_client",
            lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
        )

        reverted = revert_timed_out_tasks()

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert ticket_id not in reverted

        events = read_events(
            consumer="test-ul-dirty-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        dirty_events = [
            e
            for e in events
            if e.payload.get("paused_status") == _DIRTY_WORKTREE_REASON
        ]
        assert len(dirty_events) == 1
        needs_salvage_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert needs_salvage_events == []

        reloaded2 = load_state()
        s2 = next(s for s in reloaded2.sessions if s.id == "sess-ul-dirty")
        assert s2.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF

    def test_revert_timed_out_preserves_worktree_for_usage_limit_salvage_origin(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mandatory regression (#1336): a usage-limit-cutoff-stamped session
        must NOT have its worktree deleted by the revert_timed_out_tasks
        backstop, and a clean worktree must still auto-retry (RUNNING→PENDING)."""
        worktree = tmp_path / "wt-ul-preserve"
        worktree.mkdir(parents=True)
        (worktree / "committed.txt").write_text("work")
        ticket_id = "TKT-UL-PRESERVE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-ul-preserve", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ul-preserve",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.detect_usage_limit", lambda _s: _RECENT_UL
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            (
                "sess-ul-preserve",
                ticket_id,
                "dev/ul-preserve-branch",
                str(worktree),
                False,
            )
        ]
        salvage_committed_no_pr_sessions(candidates)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-ul-preserve")
        assert s.status == SessionStatus.TIMED_OUT
        assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF

        remove_worktree_calls: list[object] = []
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree",
            lambda *a, **kw: remove_worktree_calls.append((a, kw)),
        )

        # Clean worktree (not a real git repo → checked_out_branch returns
        # None → not dirty) — no dirty patch needed here.
        reverted = revert_timed_out_tasks()

        assert remove_worktree_calls == []
        assert worktree.exists()
        assert (worktree / "committed.txt").exists()

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None
        assert ticket_id in reverted

    def test_low_path_merges_into_existing_last_result(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW path must MERGE paused_status into last_result, not replace it —
        otherwise it destroys the sentinel `status` that `cw dev-queue approve`
        requires (#1105)."""
        from cw.reconcile.salvage import _salvage_low_path

        worktree = tmp_path / "wt-merge"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-MERGE"
        sess = _mk_live_daemon_session_with_worktree("sess-merge", worktree, ticket_id)
        sess.last_result = {
            "status": "review_pending_approval",
            "review": {"clean": True},
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-merge",
                    )
                ]
            )
        )
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        _salvage_low_path(sess, ticket_id, "dev/merge-branch", str(worktree))

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-merge")
        assert s.last_result is not None
        assert s.last_result["status"] == "review_pending_approval"
        assert s.last_result["review"] == {"clean": True}
        assert s.last_result["paused_status"] == _NEEDS_SALVAGE_REASON
        assert s.reap_reason == ReapReason.SALVAGE_PARKED
        assert s.status == SessionStatus.COMPLETED

    def test_stalled_needs_salvage_route_stamps_disposition(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW-path salvage's bare transition_task_status call stamps disposition
        (#976 Bug B — salvage.py bare-site regression)."""
        worktree = tmp_path / "wt-low-disp"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-LOW-DISP"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-low-disp", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-low-disp",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-low-disp", ticket_id, "dev/low-disp-branch", str(worktree), False)
        ]
        salvage_committed_no_pr_sessions(candidates)

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == _NEEDS_SALVAGE_REASON

    def test_low_path_idempotent_on_second_pass(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_salvage_low_path called twice for same already-flagged session →
        SESSION_NEEDS_ATTENTION fires exactly once, fire_push_notification
        called exactly once. Guards the self-contained idempotency added in #418."""
        worktree = tmp_path / "wt-idem-low"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-IDEM-LOW"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-idem-low", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-idem-low",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        push_calls: list[tuple[str, str]] = []

        def _capture_push(name: str, client: str, **_kw: object) -> None:
            push_calls.append((name, client))

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates = [
            ("sess-idem-low", ticket_id, "dev/idem-low-branch", str(worktree), False)
        ]

        # First pass — should flag and emit.
        salvage_committed_no_pr_sessions(candidates)
        # Second pass — already_flagged should suppress.
        salvage_committed_no_pr_sessions(candidates)

        events = read_events(
            consumer="test-low-idem",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert len(attn_events) == 1, "SESSION_NEEDS_ATTENTION must fire exactly once"
        assert len(push_calls) == 1, (
            "fire_push_notification must be called exactly once"
        )

    def test_idempotency_recheck_pr_now_exists_downgrades_to_low(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PR appears on the second pr_exists_for_branch call → downgrade to LOW."""
        worktree = tmp_path / "wt-idem"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-IDEM"
        sess = _mk_live_daemon_session_with_worktree("sess-idem", worktree, ticket_id)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-idem",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        call_count = [0]

        def _pr_exists_side_effect(branch: str, **_kw: object) -> tuple[bool, bool]:
            call_count[0] += 1
            if call_count[0] == 1:
                return False, True  # outer check: no PR
            return True, True  # idempotency re-check: PR now exists

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", _pr_exists_side_effect
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-idem", ticket_id, "dev/idem-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        # Downgraded to LOW — no PR created, task blocked
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

        # No git push attempted
        # (confirmed by no SESSION_COMPLETED with salvage_kind=git_state_salvage)
        events = read_events(
            consumer="test-idem-recheck",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert all(
            e.payload.get("salvage_kind") != _SALVAGE_KIND_GIT_STATE for e in events
        )

    def test_gh_unavailable_skips_salvage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr_exists_for_branch returns (None, False) → no salvage, no event."""
        worktree = tmp_path / "wt-gh-absent"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-GHABS"
        sess = _mk_live_daemon_session_with_worktree("sess-ghabs", worktree, ticket_id)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ghabs",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (None, False)
        )

        candidates = [
            ("sess-ghabs", ticket_id, "dev/ghabs-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING  # unchanged

        events = read_events(
            consumer="test-gh-absent-salvage",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not events

    def test_no_commits_beyond_base_skips_salvage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_has_commits_beyond_base returns False → no salvage."""
        worktree = tmp_path / "wt-no-commits"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-NOCOMMITS"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-nocommits", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-nocommits",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: False
        )

        candidates = [
            ("sess-nocommits", ticket_id, "dev/nc-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING  # unchanged

    def test_worktree_not_deleted_for_salvage_candidates(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sessions in salvage_git list do NOT have remove_worktree called."""
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        worktree = tmp_path / "wt-nodelete"
        worktree.mkdir(parents=True)
        (worktree / ".claude").mkdir()
        (worktree / ".claude" / "cw-context.json").write_text(
            '{"headless": true, "session_id": "sess-nodelete"}'
        )

        ticket_id = "TKT-NODELETE"
        sess = Session(
            id="sess-nodelete",
            name=f"client-a/auto-dev/{ticket_id}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="live-ref",
            started_at=started_at,
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-nodelete",
                        attempts=5,  # at/above cap → would normally park
                    )
                ]
            )
        )

        remove_worktree_calls: list[object] = []

        def _mock_remove(*args: object, **kwargs: object) -> None:
            remove_worktree_calls.append(args)

        monkeypatch.setattr("cw.reconcile._shared.remove_worktree", _mock_remove)
        # Patch _checked_out_branch to return a valid branch (triggers salvage_git)
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: "dev/nodelete-branch",
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.salvage_terminal_result", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.reconcile.idle._detect._transcript_recently_active",
            lambda *_a, **_kw: False,
        )
        monkeypatch.setattr(
            "cw.reconcile.idle._detect._awaiting_subagent", lambda *_a, **_kw: False
        )

        state = CwState(sessions=[sess])
        _, salvage_git = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

        # Session ended up in salvage_git, not park
        assert len(salvage_git) == 1
        assert salvage_git[0][0] == "sess-nodelete"

        # remove_worktree was NOT called
        assert remove_worktree_calls == []

    def test_double_fire_guard_skips_needs_salvage_sessions(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """revert_stalled_headless_sessions skips sessions with
        last_result={"paused_status": "needs_salvage"}."""
        worktree = tmp_path / "wt-dblfire"
        worktree.mkdir(parents=True)
        (worktree / ".claude").mkdir()
        (worktree / ".claude" / "cw-context.json").write_text(
            '{"headless": true, "session_id": "sess-dblfire"}'
        )

        ticket_id = "TKT-DBLFIRE"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        sess = Session(
            id="sess-dblfire",
            name=f"client-a/auto-dev/{ticket_id}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="live-ref",
            started_at=started_at,
            last_result={"paused_status": _NEEDS_SALVAGE_REASON},
        )
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.BLOCKED_ON_USER,
                        session_id="sess-dblfire",
                    )
                ]
            )
        )

        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config()
        )

        # Session was skipped — not reverted to PENDING
        assert ticket_id not in reverted
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_time_window_stale_event_does_not_trigger_high(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale s3_review_complete event (before session.started_at) → LOW path."""
        from cw.events import record_event as _record_event

        worktree = tmp_path / "wt-timewindow"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-TIMEWINDOW"
        # Session started AFTER the event was recorded
        # (simulate: event is from a prior session)
        started_at = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
        sess = Session(
            id="sess-timewindow",
            name=f"client-a/auto-dev/{ticket_id}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="live-ref",
            started_at=started_at,
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # Write event with a timestamp AFTER session started
        # (but _detect_post_review_clean uses since_ts=session.started_at
        #  and checks session_id match)
        # The event has a DIFFERENT session_id — should not trigger HIGH
        _record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "different-session-id",  # wrong session
                "stage": _STAGE_REVIEW_COMPLETE,
            },
        )

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        # post_review_clean=False (different session_id → _detect_post_review_clean
        # returns False → LOW path)
        candidates = [
            ("sess-timewindow", ticket_id, "dev/tw-branch", str(worktree), False)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_session_not_in_state_is_skipped(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Candidate session_id not found in state → silently skipped."""
        save_state(CwState(sessions=[]))  # empty — no sessions
        save_dev_queue(DevQueueStore(tasks=[]))

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )

        candidates = [
            (
                "sess-missing",
                "TKT-MISSING",
                "dev/missing-branch",
                str(tmp_path),
                True,
            )
        ]
        completed = salvage_committed_no_pr_sessions(candidates)
        assert completed == []

    def test_pr_transient_error_skips_candidate(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr_exists_for_branch returns (None, True) → skip candidate."""
        worktree = tmp_path / "wt-transient"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-TRANSIENT"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-transient", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-transient",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        # (None, True) = transient error, gh available
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (None, True)
        )

        completed = salvage_committed_no_pr_sessions(
            [("sess-transient", ticket_id, "dev/t-branch", str(worktree), True)]
        )
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

    def test_pr_already_exists_skips_candidate(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr_exists_for_branch returns (True, True) → PR exists, skip."""
        worktree = tmp_path / "wt-prexists"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-PREXISTS"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-prexists", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-prexists",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (True, True)
        )

        completed = salvage_committed_no_pr_sessions(
            [("sess-prexists", ticket_id, "dev/pe-branch", str(worktree), True)]
        )
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

    def test_salvage_skips_session_with_unknown_client(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Unknown client → CwError caught, session skipped, completed empty."""
        worktree = tmp_path / "wt-unknown-client"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-UNKNOWNCLIENT"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-unknownclient", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-unknownclient",
                    )
                ]
            )
        )
        # Intentionally no _write_staged_clients_yaml call → get_client raises CwError

        completed = salvage_committed_no_pr_sessions(
            [("sess-unknownclient", ticket_id, "dev/uc-branch", str(worktree), True)]
        )

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING  # unchanged — session skipped

    def test_git_push_failure_downgrades_to_low(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """git push failure in HIGH path → downgrade to LOW (BLOCKED_ON_USER)."""
        worktree = tmp_path / "wt-pushfail"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-PUSHFAIL"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-pushfail", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-pushfail",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        def _subprocess_push_fails(args: list[str], **_kw: object) -> None:
            if args[:2] == ["git", "push"]:
                raise subprocess.CalledProcessError(1, args)
            msg = f"unexpected call: {args}"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.subprocess.run", _subprocess_push_fails
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        completed = salvage_committed_no_pr_sessions(
            [("sess-pushfail", ticket_id, "dev/pf-branch", str(worktree), True)]
        )
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_gh_pr_create_failure_downgrades_to_low(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh pr create failure in HIGH path → downgrade to LOW (BLOCKED_ON_USER)."""
        worktree = tmp_path / "wt-createfail"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-CREATEFAIL"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-createfail", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-createfail",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        def _subprocess_create_fails(args: list[str], **_kw: object) -> MagicMock:
            if args[:2] == ["git", "push"]:
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                return result
            if args[:2] == ["gh", "pr"]:
                raise subprocess.CalledProcessError(1, args)
            msg = f"unexpected call: {args}"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.subprocess.run", _subprocess_create_fails
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        completed = salvage_committed_no_pr_sessions(
            [("sess-createfail", ticket_id, "dev/cf-branch", str(worktree), True)]
        )
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_high_path_uses_client_default_branch_not_main(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HIGH path uses client's default_branch in gh pr create.

        Regression: hardcoded 'main' was replaced by the client's
        default_branch in the --base arg.
        """
        worktree = tmp_path / "wt-devbranch"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-DEVBRANCH"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-devbranch", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-devbranch",
                    )
                ]
            )
        )

        # Write a client config with default_branch=develop (not main)
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-staged\n"
            "    default_branch: develop\n"
            "    pipeline:\n"
            "      stages: [plan, impl, review, finalize]\n"
        )

        _write_stage_event("sess-devbranch", _STAGE_REVIEW_COMPLETE, sess.started_at)

        gh_base_args: list[str] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            result = MagicMock()
            result.returncode = 0
            if args[:2] == ["git", "push"]:
                result.stdout = ""
                return result
            if args[:2] == ["gh", "pr"]:
                gh_base_args.extend(args)
                result.stdout = "https://github.com/org/repo/pull/77\n"
                return result
            return result

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_subprocess_run)
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        completed = salvage_committed_no_pr_sessions(
            [("sess-devbranch", ticket_id, "dev/devbranch", str(worktree), True)]
        )

        assert ticket_id in completed
        # Verify --base uses the client's default_branch, not "main"
        assert "--base" in gh_base_args
        base_idx = gh_base_args.index("--base")
        assert gh_base_args[base_idx + 1] == "develop"

    def test_salvage_committed_no_pr_skips_merged_ticket(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids short-circuits before pr_exists_for_branch (#1054).

        A ticket whose PR already merged is shipped ground truth -- no PR
        should be created, and it must not appear in the completed list
        (completion is owned by the idle merged path, not this salvage post-pass).
        """
        worktree = tmp_path / "wt-merged-salvage"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-MERGED-SALVAGE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-merged-salvage", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-merged-salvage",
                    )
                ]
            )
        )
        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        pr_check = MagicMock(return_value=(False, True))
        monkeypatch.setattr("cw.reconcile.salvage.pr_exists_for_branch", pr_check)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-merged-salvage", ticket_id, "dev/merged-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(
            candidates, merged_client_ticket_ids=frozenset({("client-a", ticket_id)})
        )

        assert ticket_id not in completed
        pr_check.assert_not_called()

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

    def test_salvage_committed_no_pr_different_client_ticket_not_skipped(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_client_ticket_ids for a DIFFERENT client must not skip this
        session's same-numbered ticket (cross-client collision guard, #1054).
        """
        worktree = tmp_path / "wt-collision-salvage"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-COLLIDE-SALVAGE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-collide-salvage", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-collide-salvage",
                    )
                ]
            )
        )
        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        pr_check = MagicMock(return_value=(False, True))
        monkeypatch.setattr("cw.reconcile.salvage.pr_exists_for_branch", pr_check)
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            (
                "sess-collide-salvage",
                ticket_id,
                "dev/collide-branch",
                str(worktree),
                False,  # post_review_clean=False -> LOW path, no gh subprocess
            )
        ]
        # client-b's same-numbered ticket is merged; this session is client-a's.
        salvage_committed_no_pr_sessions(
            candidates, merged_client_ticket_ids=frozenset({("client-b", ticket_id)})
        )

        pr_check.assert_called_once()


# ---------------------------------------------------------------------------
# TestDetectPostReviewClean
# ---------------------------------------------------------------------------


def test_reap_reason_salvage_completed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_salvage_high_path sets reap_reason=salvage_completed on the session."""
    worktree = tmp_path / "wt-salv-comp"
    worktree.mkdir(parents=True)
    ticket_id = "SALV-COMP-1"

    sess = Session(
        id="salv-comp-1",
        name=f"client-a/auto-dev/{ticket_id}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref="live-ref-sc",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-comp-1",
                )
            ]
        )
    )

    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = "https://github.com/org/repo/pull/99\n"
        return result

    monkeypatch.setattr(
        "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
    )
    monkeypatch.setattr(
        "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
    )
    monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

    candidates: list[tuple[str, str | None, str, str, bool]] = [
        ("salv-comp-1", ticket_id, "dev/salv-comp", str(worktree), True)
    ]
    salvage_committed_no_pr_sessions(candidates)

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "salv-comp-1")
    assert s.reap_reason == ReapReason.SALVAGE_COMPLETED


def test_reap_reason_salvage_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_salvage_low_path sets reap_reason=salvage_parked on the session."""
    worktree = tmp_path / "wt-salv-park"
    worktree.mkdir(parents=True)
    ticket_id = "SALV-PARK-1"

    sess = Session(
        id="salv-park-1",
        name=f"client-a/auto-dev/{ticket_id}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref="live-ref-sp",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-park-1",
                )
            ]
        )
    )

    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    monkeypatch.setattr(
        "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
    )
    monkeypatch.setattr(
        "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
    )
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

    candidates: list[tuple[str, str | None, str, str, bool]] = [
        ("salv-park-1", ticket_id, "dev/salv-park", str(worktree), False)
    ]
    salvage_committed_no_pr_sessions(candidates)

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "salv-park-1")
    assert s.reap_reason == ReapReason.SALVAGE_PARKED
    assert s.status == SessionStatus.COMPLETED


# ---------------------------------------------------------------------------
# Phase detect/act split tests (GitHub #552)
# ---------------------------------------------------------------------------


# --- _detect_stalled_candidates ---


class TestFinalizeBlocked:
    """Tests for finalize-blocked detection + rescue (GitHub #812)."""

    # ── shared setup helpers ──────────────────────────────────────────────

    def _mk_finalize_session(
        self, sid: str, ticket_id: str, worktree: Path, started_at: datetime
    ) -> Session:
        """Return an ACTIVE DAEMON FINALIZE-stage session past budget.

        Name uses ticket_id (not sid) so ticket_id_for_session() resolves correctly.
        """
        sess = _make_daemon_session(
            id=sid,
            name=f"client-a/auto-dev/{ticket_id}",
            worktree_path=worktree,
            surface_ref="surf-ref",
            started_at=started_at,
        )
        context_dir = worktree / ".claude"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "cw-context.json").write_text(
            '{"headless": true, "session_id": "' + sid + '"}'
        )
        return sess

    def _mk_finalize_task(
        self, ticket_id: str, sid: str, *, lane: str = DEFAULT_LANE
    ) -> TicketTask:
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=sid,
            stage=Stage.FINALIZE,
            lane=lane,
        )

    # ── 1.1 happy path ───────────────────────────────────────────────────

    def test_finalize_blocked_happy_path(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FINALIZE + commits + no PR → TIMED_OUT, BLOCKED_ON_USER, FINALIZE_BLOCKED."""
        worktree = tmp_path / "wt-fb-1"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-1"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-1", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = self._mk_finalize_task(ticket_id, "fb-sess-1", lane="finalize-lane")
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        push_calls: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification",
            lambda name, _client: push_calls.append(name),
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch",
            lambda _b, **_kw: (False, True),
        )

        reverted = revert_stalled_headless_sessions(
            load_state(), now=now, config=_auto_config()
        )

        assert ticket_id not in reverted  # not reverted to PENDING

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "fb-sess-1")
        assert s.status == SessionStatus.TIMED_OUT
        assert s.reap_reason == ReapReason.FINALIZE_BLOCKED
        assert isinstance(s.last_result, dict)
        assert s.last_result.get("paused_status") == "finalize_blocked"
        assert "dev/FB-1" in str(s.last_result.get("branch", ""))

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.session_id is None

        assert push_calls  # push notification fired

        events = read_events(
            consumer="test-fb-1-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "finalize-lane"

    # ── 1.2 no commits → REVERT_TASK ─────────────────────────────────────

    def test_no_commits_falls_through_to_revert(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No commits beyond base → REVERT_TASK (not FINALIZE_BLOCKED)."""
        worktree = tmp_path / "wt-fb-2"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-2"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-2", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-2")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base",
            lambda _p, _b: False,
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_a, **_kw: (False, True),
        )

        reverted = revert_stalled_headless_sessions(
            load_state(), now=now, config=_auto_config()
        )

        assert ticket_id in reverted  # reverted to PENDING (REVERT_TASK path)
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING

    # ── 1.3 PR exists → REVERT_TASK ──────────────────────────────────────

    def test_pr_exists_falls_through_to_revert(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PR already open → REVERT_TASK (not FINALIZE_BLOCKED)."""
        worktree = tmp_path / "wt-fb-3"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-3"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-3", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-3")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        # PR exists → (True, True): not finalize-blocked
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch",
            lambda _b, **_kw: (True, True),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_a, **_kw: (False, True),
        )

        reverted = revert_stalled_headless_sessions(
            load_state(), now=now, config=_auto_config()
        )

        assert ticket_id in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING

    # ── 1.4 Stage.IMPL → REVERT_TASK ─────────────────────────────────────

    def test_impl_stage_not_finalize_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stage.IMPL → REVERT_TASK regardless of commits/PR state."""
        worktree = tmp_path / "wt-fb-4"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-4"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-4", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        impl_task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="fb-sess-4",
            stage=Stage.IMPL,  # IMPL, not FINALIZE
        )
        save_dev_queue(DevQueueStore(tasks=[impl_task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch",
            lambda _b, **_kw: (False, True),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_a, **_kw: (False, True),
        )

        reverted = revert_stalled_headless_sessions(
            load_state(), now=now, config=_auto_config()
        )

        assert ticket_id in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING

    # ── 1.5 gh unavailable → _GH_CHECK_BLOCKED_REASON ───────────────────

    def test_gh_unavailable_routes_to_gh_check_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh absent → fall-through to REVERT_TASK → gh_check_blocked."""
        worktree = tmp_path / "wt-fb-5"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-5"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-5", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-5")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        # gh unavailable in _resolve_finalize_blocked_condition
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch",
            lambda _b, **_kw: (None, False),
        )
        # gh also unavailable in revert_stalled_headless_sessions pre-pass
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_a, **_kw: (None, False),
        )

        revert_stalled_headless_sessions(load_state(), now=now, config=_auto_config())

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        # gh_blocked_revert_candidates path → BLOCKED_ON_USER
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

        events = read_events(
            consumer="test-fb-5-needs-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn = [e for e in events if e.payload.get("ticket_id") == ticket_id]
        assert any(e.payload.get("paused_status") == "gh_check_blocked" for e in attn)

    # ── 1.6 worktree NOT cleaned up ──────────────────────────────────────

    def test_worktree_preserved(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Finalize-blocked: _cleanup_timed_out_worktree is NOT called."""
        worktree = tmp_path / "wt-fb-6"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-6"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-6", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-6")])
        )

        cleanup_calls: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._events._cleanup_timed_out_worktree",
            lambda s, _tid: cleanup_calls.append(s.id),
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch",
            lambda _b, **_kw: (False, True),
        )

        revert_stalled_headless_sessions(load_state(), now=now, config=_auto_config())

        assert "fb-sess-6" not in cleanup_calls

    # ── 1.7 breadcrumbs contains branch ──────────────────────────────────

    def test_needs_attention_breadcrumbs_contains_branch(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SESSION_NEEDS_ATTENTION breadcrumbs contain the feature branch name."""
        worktree = tmp_path / "wt-fb-7"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-7"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-7", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-7")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch",
            lambda _b, **_kw: (False, True),
        )

        revert_stalled_headless_sessions(load_state(), now=now, config=_auto_config())

        events = read_events(
            consumer="test-fb-7-breadcrumbs",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn = [
            e
            for e in events
            if e.payload.get("paused_status") == "finalize_blocked"
            and e.payload.get("ticket_id") == ticket_id
        ]
        assert len(attn) == 1
        assert "dev/FB-7" in str(attn[0].payload.get("breadcrumbs", ""))

    # ── 1.8 idempotency ──────────────────────────────────────────────────

    def test_idempotency_second_tick_skips(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second tick: session is TIMED_OUT → skipped by _LIVE_STATUSES guard."""
        worktree = tmp_path / "wt-fb-8"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-8"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-8", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-8")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch",
            lambda _b, **_kw: (False, True),
        )

        # Tick 1: detect + act → TIMED_OUT
        revert_stalled_headless_sessions(load_state(), now=now, config=_auto_config())

        # Tick 2: session is TIMED_OUT; detect returns no candidates for it
        from cw.reconcile.stalled import _detect_stalled_candidates

        state2 = load_state()
        candidates2 = _detect_stalled_candidates(
            state2,
            now=now,
            config=_auto_config(),
            task_by_ticket={t.ticket_id: t for t in load_dev_queue().tasks},
        )
        fb_candidates = [c for c in candidates2 if c.session_id == "fb-sess-8"]
        assert not fb_candidates  # TIMED_OUT session skipped by _LIVE_STATUSES

    # ── 1.9 unit test for _resolve_finalize_blocked_condition ─────────────

    def test_resolve_condition_true_for_finalize_false_for_impl(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_resolve_finalize_blocked_condition: True for FINALIZE, False for IMPL."""
        from cw.reconcile.stalled import _resolve_finalize_blocked_condition

        worktree = tmp_path / "wt-fb-9"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-9", "FB-9", worktree, started_at)
        finalize_task = self._mk_finalize_task("FB-9", "fb-sess-9")
        impl_task = TicketTask(
            ticket_id="FB-9",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="fb-sess-9",
            stage=Stage.IMPL,
        )

        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        pr_cwds: list[object] = []

        def _capture_pr_exists(_b: str, **kw: object) -> tuple[bool | None, bool]:
            pr_cwds.append(kw.get("cwd"))
            return False, True

        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch", _capture_pr_exists
        )

        blocked, branch = _resolve_finalize_blocked_condition(
            finalize_task, sess, worktree, "main"
        )
        assert blocked is True
        assert branch is not None
        assert "FB-9" in branch
        # Fallback path gh call scoped to client-a's repo cwd (#1279).
        assert pr_cwds == [Path("/tmp/ws-staged")]

        blocked2, branch2 = _resolve_finalize_blocked_condition(
            impl_task, sess, worktree, "main"
        )
        assert blocked2 is False
        assert branch2 is None

    def test_resolve_condition_dangling_client_skips_gh_returns_false(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fallback path with a dangling client (clients.yaml populated but
        missing session.client) → pr_exists_for_branch NOT called, returns
        (False, None) → REVERT_TASK fallthrough (GitHub #1279 R7)."""
        from cw.reconcile.stalled import _resolve_finalize_blocked_condition

        worktree = tmp_path / "wt-fb-dangling"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        # clients.yaml populated with a DIFFERENT client than the session's.
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        sess = self._mk_finalize_session("fb-dangling", "FB-D", worktree, started_at)
        finalize_task = self._mk_finalize_task("FB-D", "fb-dangling")

        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        called: list[str] = []

        def _should_not_run(_b: str, **_kw: object) -> tuple[bool | None, bool]:
            called.append(_b)
            return False, True

        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch", _should_not_run
        )

        blocked, branch = _resolve_finalize_blocked_condition(
            finalize_task, sess, worktree, "main"
        )
        assert blocked is False
        assert branch is None
        assert called == []

    # ── 1.10 rescue happy path ────────────────────────────────────────────

    def test_rescue_happy_path(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rescue_finalize_blocked_sessions: gh pr create + merge, task COMPLETED."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-10"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-10"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # TIMED_OUT session with finalize-blocked marker
        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        gh_args_seen: list[list[str]] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            gh_args_seen.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_subprocess_run)
        pr_cwds: list[object] = []

        def _capture_pr_exists(_b: str, **kw: object) -> tuple[bool | None, bool]:
            pr_cwds.append(kw.get("cwd"))
            return False, True

        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", _capture_pr_exists
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions()

        assert ticket_id in completed
        # gh call scoped to client-a's repo, not the ambient CWD (#1279).
        assert pr_cwds == [Path("/tmp/ws-staged")]

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.status == SessionStatus.COMPLETED
        assert s.reap_reason == ReapReason.FINALIZE_BLOCKED

        # Verify gh pr create and gh pr merge were called
        create_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "create"]]
        merge_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "merge"]]
        assert create_calls
        assert merge_calls

        events = read_events(
            consumer="test-fb-10-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        rescue_events = [
            e
            for e in events
            if e.payload.get("salvage_kind") == "finalize_blocked_rescue"
        ]
        assert len(rescue_events) == 1

    # ── 1.10b merged ticket parked finalize-blocked → complete, no PR ──────

    def test_rescue_finalize_blocked_merged_during_park_no_duplicate_pr(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids short-circuits before the OPEN-only pr_exists_for_branch
        check (#1054) -- completes the session/task directly without opening or
        merging a PR (skip_merge=True), avoiding a duplicate _rescue_open_pr call."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-MERGED-1"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-merged-1"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        gh_args_seen: list[list[str]] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            gh_args_seen.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_subprocess_run)
        pr_check = MagicMock(return_value=(False, True))
        monkeypatch.setattr("cw.reconcile.salvage.pr_exists_for_branch", pr_check)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions(
            merged_client_ticket_ids=frozenset({("client-a", ticket_id)})
        )

        assert ticket_id in completed
        pr_check.assert_not_called()

        create_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "create"]]
        merge_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "merge"]]
        assert create_calls == []
        assert merge_calls == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.status == SessionStatus.COMPLETED

        events = read_events(
            consumer=f"test-rescue-merged-{sid}",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload.get("skip_merge") is True

    def test_rescue_finalize_blocked_merged_does_not_complete_other_clients_task(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_complete's dev-queue task lookup is (client, ticket_id)
        scoped (#1054): when a DIFFERENT client also has a BLOCKED_ON_USER
        task sharing this ticket_id string, only the merged session's own
        client's task is completed -- the other client's task must be left
        untouched, not silently marked COMPLETED by the bare ticket_id match
        that pre-existed in _rescue_complete."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-COLLIDE-COMPLETE-1"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-collide-complete-1"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task_a = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        # client-b's own BLOCKED_ON_USER task happens to share this ticket_id
        # string -- must survive untouched. Listed BEFORE task_a: _rescue_complete
        # `break`s on its first dev-queue match, so without client-scoping this
        # ordering would complete task_b (wrong client) and never reach task_a.
        task_b = TicketTask(
            ticket_id=ticket_id,
            client="client-b",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task_b, task_a]))

        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            MagicMock(return_value=MagicMock(returncode=0, stdout="")),
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch",
            MagicMock(return_value=(False, True)),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions(
            merged_client_ticket_ids=frozenset({("client-a", ticket_id)})
        )

        assert ticket_id in completed

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
        # The regression this test guards against: pre-fix, _rescue_complete's
        # bare `task.ticket_id == ticket_id` match (no client filter) would
        # complete whichever BLOCKED_ON_USER task it iterated to first.
        assert reloaded_b.status == QueueItemStatus.BLOCKED_ON_USER

    def test_rescue_finalize_blocked_different_client_ticket_not_completed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_client_ticket_ids for a DIFFERENT client must not complete
        this session's same-numbered ticket (cross-client collision guard,
        #1054) -- the normal PR-open flow must run instead."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-COLLIDE-1"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-collide-1"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        pr_check = MagicMock(return_value=(False, True))
        monkeypatch.setattr("cw.reconcile.salvage.pr_exists_for_branch", pr_check)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        # No existing PR + not merged-for-this-client -> falls through to
        # _rescue_open_pr's real gh subprocess call; force it to fail so the
        # session is tombstoned as rescue_attempted rather than completed.
        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, ["gh"])),
        )

        # client-b's same-numbered ticket is merged; this session is client-a's.
        rescue_finalize_blocked_sessions(
            merged_client_ticket_ids=frozenset({("client-b", ticket_id)})
        )

        # Falls through to the normal PR-existence check rather than the
        # merged-ticket skip_merge=True completion shortcut.
        pr_check.assert_called_once()

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.status == SessionStatus.TIMED_OUT

    # ── 1.11 PR already exists → skip create, still merge ─────────────────

    def test_rescue_pr_already_exists(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PR already open → skip gh pr create, still call gh pr merge."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-11"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-11"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        gh_args_seen: list[list[str]] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            gh_args_seen.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_subprocess_run)
        # PR already exists → (True, True)
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (True, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions()

        assert ticket_id in completed

        create_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "create"]]
        merge_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "merge"]]
        assert not create_calls  # create skipped
        assert merge_calls  # merge still called

    # ── 1.12 gh pr create fails → rescue_attempted, task stays BLOCKED ───

    def test_rescue_create_fails_marks_attempted(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh pr create fails → rescue_attempted=True, task stays BLOCKED_ON_USER."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-12"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-12"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        import subprocess as _subprocess

        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            lambda _args, **_kw: (_ for _ in ()).throw(
                _subprocess.CalledProcessError(1, "gh")
            ),
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions()

        assert ticket_id not in completed

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER  # unchanged

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert isinstance(s.last_result, dict)
        assert s.last_result.get("rescue_attempted") is True

    # ── 1.13 _rescue_mark_attempted: non-dict last_result ────────────────

    def test_rescue_mark_attempted_non_dict_last_result(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_mark_attempted: when last_result is not a dict, set to dict."""
        from cw.reconcile.salvage import _rescue_mark_attempted

        sid = "fb-sess-13"
        ticket_id = "FB-13"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        # last_result=None (default) → else branch sets it to {"rescue_attempted": True}
        save_state(CwState(sessions=[sess]))

        _rescue_mark_attempted(sid)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.last_result == {"rescue_attempted": True}

    # ── 1.14 _rescue_complete: not-mutated early return (race) ───────────

    def test_rescue_complete_not_mutated_returns_early(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_complete: session already COMPLETED under lock → no event emitted."""
        from cw.reconcile.salvage import _rescue_complete

        sid = "fb-sess-14"
        ticket_id = "FB-14"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # State has session already COMPLETED (simulates concurrent completion).
        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.status = SessionStatus.COMPLETED
        save_state(CwState(sessions=[sess]))

        completed_ticket_ids: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        # _rescue_complete calls gh pr merge BEFORE the lock guard; patch to
        # avoid a real subprocess invocation in this race-condition test path.
        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            lambda *_a, **_kw: MagicMock(returncode=0, stdout=""),
        )

        stale_sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        _rescue_complete(stale_sess, ticket_id, branch, completed_ticket_ids)

        assert not completed_ticket_ids
        events = read_events(
            consumer="test-fb-14-no-event",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("session_id") == sid for e in events)

    # ── 1.15 _rescue_complete: surface_ref set → daemon stop ─────────────

    def test_rescue_complete_daemon_stop_on_surface_ref(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_complete: surface_ref present → daemon.stop() is called."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-15"
        ticket_id = "FB-15"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        sess.surface_ref = "surf-ref-15"
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            lambda _a, **_kw: MagicMock(returncode=0, stdout=""),
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )

        rescue_finalize_blocked_sessions()

        assert daemon.stop_calls

    # ── 1.16 merge failure swallowed, session still completed ─────────────

    def test_rescue_merge_failure_swallowed(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh pr merge CalledProcessError is swallowed; session still COMPLETED."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-16"
        ticket_id = "FB-16"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.BLOCKED_ON_USER,
                    )
                ]
            )
        )

        import subprocess as _sub

        def _fake_run(args: list[str], **_kw: object) -> MagicMock:
            if args[:3] == ["gh", "pr", "merge"]:
                raise _sub.CalledProcessError(1, "gh")
            return MagicMock(returncode=0, stdout="")

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions()

        assert ticket_id in completed
        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.status == SessionStatus.COMPLETED

    # ── 1.17 rescue filter edge cases ────────────────────────────────────

    def test_rescue_filter_wrong_paused_status(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """rescue_finalize_blocked_sessions: wrong paused_status → session skipped."""
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17a"
        ticket_id = "FB-17A"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.last_result = {
            "paused_status": "some_other_reason",
            "branch": f"dev/{ticket_id}",
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        completed = rescue_finalize_blocked_sessions()
        assert ticket_id not in completed

    def test_rescue_filter_rescue_attempted(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """rescue_finalize_blocked_sessions: rescue_attempted=True → session skipped."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17b"
        ticket_id = "FB-17B"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.last_result = {
            "paused_status": _FINALIZE_BLOCKED_REASON,
            "branch": f"dev/{ticket_id}",
            "rescue_attempted": True,
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        completed = rescue_finalize_blocked_sessions()
        assert ticket_id not in completed

    def test_rescue_filter_empty_branch(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """rescue_finalize_blocked_sessions: empty branch → session skipped."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17c"
        ticket_id = "FB-17C"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.last_result = {
            "paused_status": _FINALIZE_BLOCKED_REASON,
            "branch": "",
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        completed = rescue_finalize_blocked_sessions()
        assert ticket_id not in completed

    def test_rescue_filter_unknown_client(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """rescue_finalize_blocked_sessions: unknown client → log warning, skip."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17d"
        ticket_id = "FB-17D"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.client = "no-such-client"
        sess.name = f"no-such-client/auto-dev/{ticket_id}"
        sess.last_result = {
            "paused_status": _FINALIZE_BLOCKED_REASON,
            "branch": f"dev/{ticket_id}",
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        completed = rescue_finalize_blocked_sessions()
        assert ticket_id not in completed

    def test_rescue_filter_gh_unavailable(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh unavailable → session skipped, rescue_attempted not written."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17e"
        ticket_id = "FB-17E"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (None, False)
        )

        completed = rescue_finalize_blocked_sessions()

        assert completed == []
        # gh-unavailable is transient — rescue_attempted must NOT be tombstoned.
        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert not (
            isinstance(s.last_result, dict) and s.last_result.get("rescue_attempted")
        )

    # ── 1.18 _resolve_finalize_blocked_condition with pre-computed dict ───

    def test_resolve_uses_finalize_pr_by_branch_dict(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_resolve_finalize_blocked_condition uses pre-computed dict when provided."""
        from cw.reconcile.stalled import _resolve_finalize_blocked_condition

        worktree = tmp_path / "wt-fb-18"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-18", "FB-18", worktree, started_at)
        task = self._mk_finalize_task("FB-18", "fb-sess-18")

        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )

        branch = "dev/FB-18"
        finalize_pr_by_branch = {branch: (False, True)}

        blocked, result_branch = _resolve_finalize_blocked_condition(
            task,
            sess,
            worktree,
            "main",
            finalize_pr_by_branch=finalize_pr_by_branch,
        )
        assert blocked is True
        assert result_branch == branch

    # ── 1.19 _apply_finalize_blocked_queue_mutations: filter branches ─────

    def test_apply_finalize_blocked_queue_mutations_filter_branches(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_apply_finalize_blocked_queue_mutations: filter branches."""
        from cw.models import ReapReason
        from cw.reconcile._shared import ProposedAction, ReapCandidate
        from cw.reconcile.stalled import _apply_finalize_blocked_queue_mutations

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        candidate = ReapCandidate(
            session_id="fb-sess-19",
            proposed_action=ProposedAction.PARK_FINALIZE_BLOCKED,
            ticket_id="FB-19",
            elapsed_seconds=7200,
            reap_reason=ReapReason.FINALIZE_BLOCKED,
            lane="default",
            client="client-a",
        )

        task_target = TicketTask(
            ticket_id="FB-19",
            client="client-a",
            status=QueueItemStatus.RUNNING,
        )
        task_other = TicketTask(
            ticket_id="OTHER-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
        )
        task_not_running = TicketTask(
            ticket_id="FB-19",
            client="client-a",
            status=QueueItemStatus.PENDING,
        )
        save_dev_queue(DevQueueStore(tasks=[task_other, task_not_running, task_target]))

        _apply_finalize_blocked_queue_mutations([candidate])

        store = load_dev_queue()
        target_tasks = [t for t in store.tasks if t.ticket_id == "FB-19"]
        other_tasks = [t for t in store.tasks if t.ticket_id == "OTHER-1"]

        assert other_tasks[0].status == QueueItemStatus.RUNNING
        running_target = next(
            (t for t in target_tasks if t.status == QueueItemStatus.BLOCKED_ON_USER),
            None,
        )
        assert running_target is not None
        # Non-RUNNING FB-19 task must stay PENDING (filter branch coverage).
        pending_target = next(
            (t for t in target_tasks if t.status == QueueItemStatus.PENDING),
            None,
        )
        assert pending_target is not None

    # ── 1.20 _build_finalize_pr_map direct tests ─────────────────────────

    def test_build_finalize_pr_map(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_build_finalize_pr_map: FINALIZE DAEMON session → calls pr_exists."""
        from cw.reconcile.core import _build_finalize_pr_map

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        worktree = tmp_path / "wt-fb-20"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        sess_finalize = self._mk_finalize_session(
            "fb-sess-20", "FB-20", worktree, started_at
        )
        sess_finalize.status = SessionStatus.ACTIVE

        # Session with no valid ticket_id in name → skipped (line 130)
        sess_no_tid = Session(
            id="fb-sess-20b",
            name="client-a/impl",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            started_at=started_at,
        )

        task = TicketTask(
            ticket_id="FB-20",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []
        cwds: list[object] = []

        def _fake_pr_exists(branch: str, **kw: object) -> tuple[bool | None, bool]:
            calls.append(branch)
            cwds.append(kw.get("cwd"))
            return False, True

        monkeypatch.setattr("cw.reconcile.core.pr_exists_for_branch", _fake_pr_exists)

        state = CwState(sessions=[sess_finalize, sess_no_tid])

        result = _build_finalize_pr_map(state)

        assert calls, "pr_exists_for_branch should have been called"
        assert any("FB-20" in branch for branch in calls)
        assert any(pr is False for pr, _ in result.values())
        # sess_no_tid has no valid ticket_id → excluded; only 1 branch checked.
        assert len(calls) == 1
        # cwd scoped to client-a's _git_dir (workspace_path, no repo_path) (#1279).
        assert cwds == [Path("/tmp/ws-staged")]

    def test_build_finalize_pr_map_dangling_client_skips_gh_call(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_build_finalize_pr_map: session's client absent from a populated
        clients.yaml → branch never enters result, gh never called (#1279)."""
        from cw.reconcile.core import _build_finalize_pr_map

        # clients.yaml populated with a DIFFERENT client than the session's.
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        worktree = tmp_path / "wt-fb-dangling"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        sess_finalize = self._mk_finalize_session(
            "fb-dangling", "FB-DANGLING", worktree, started_at
        )
        sess_finalize.status = SessionStatus.ACTIVE

        task = TicketTask(
            ticket_id="FB-DANGLING",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []

        def _fake_pr_exists(branch: str, **_kw: object) -> tuple[bool | None, bool]:
            calls.append(branch)
            return False, True

        monkeypatch.setattr("cw.reconcile.core.pr_exists_for_branch", _fake_pr_exists)

        result = _build_finalize_pr_map(CwState(sessions=[sess_finalize]))

        assert calls == []
        assert result == {}

    # ── 1.21 reconcile(): ticket_id=None and gh_blocked in pr_is_merged pass

    def test_reconcile_prepass_ticket_id_none_and_gh_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile(): DAEMON sessions where ticket_id is None or gh is unavailable."""

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # Session A: DAEMON with name that yields no ticket_id → line 177 continue
        sess_no_tid = Session(
            id="fb-prepass-no-tid",
            name="client-a/impl",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=None,
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            surface_ref="surf-no-tid",
        )

        # Session B: gh returns unavailable → _gh_blocked_tids.
        # Session C below hits the gh_blocked branch (_gh_available now False).
        sess_gh_blocked = Session(
            id="fb-prepass-gh-blocked",
            name="client-a/auto-dev/FB-21-A",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=None,
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            surface_ref="surf-gh-blocked",
        )

        sess_second = Session(
            id="fb-prepass-second",
            name="client-a/auto-dev/FB-21-B",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=None,
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            surface_ref="surf-second",
        )

        save_state(CwState(sessions=[sess_no_tid, sess_gh_blocked, sess_second]))
        save_dev_queue(DevQueueStore(tasks=[]))

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (None, False),
        )
        monkeypatch.setattr(
            "cw.reconcile.core.pr_exists_for_branch", lambda _b, **_kw: (None, False)
        )

        report = reconcile()
        # No tasks → nothing completed; gh-unavailable pre-pass didn't corrupt state.
        assert report.completed_ticket_ids == []

    # ── 1.22 rescued_ticket_ids field on ReconcileReport (SHOULD_FIX 2) ────

    def test_rescue_ids_land_in_rescued_ticket_ids_not_completed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile(): rescued IDs go to rescued_ticket_ids, not completed."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON

        ticket_id = "FB-22"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-22"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            lambda *_a, **_kw: MagicMock(returncode=0, stdout=""),
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (None, True),
        )
        monkeypatch.setattr(
            "cw.reconcile.core.pr_exists_for_branch", lambda _b, **_kw: (None, True)
        )
        monkeypatch.setattr("cw.reconcile._shared._claude_agents_json", list)

        report = reconcile()

        assert ticket_id in report.rescued_ticket_ids
        assert ticket_id not in report.completed_ticket_ids

    # ── 1.23 _rescue_complete: merge only after mutated guard (SHOULD_FIX 3) ──

    def test_rescue_complete_merge_skipped_on_concurrent_completion(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_complete: gh pr merge NOT called when session past TIMED_OUT."""
        from cw.reconcile.salvage import _rescue_complete

        sid = "fb-sess-23"
        ticket_id = "FB-23"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # Session is already COMPLETED — simulates concurrent completion race.
        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.status = SessionStatus.COMPLETED
        save_state(CwState(sessions=[sess]))

        merge_called: list[bool] = []

        def _fake_run(args: list[str], **_kw: object) -> MagicMock:
            if args[:3] == ["gh", "pr", "merge"]:
                merge_called.append(True)
            return MagicMock(returncode=0, stdout="")

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed_ticket_ids: list[str] = []
        stale_sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        _rescue_complete(stale_sess, ticket_id, branch, completed_ticket_ids)

        # mutated=False → returns early; gh pr merge must NOT have been called.
        assert not merge_called
        assert not completed_ticket_ids

    # ── 1.24 unknown client in finalize-blocked detect → skip (NIT 4) ───────

    def test_finalize_blocked_unknown_client_skip(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unknown client in _detect_stalled_candidates: skip, not 'main' fallback."""
        from cw.reconcile.stalled import _detect_stalled_candidates

        worktree = tmp_path / "wt-fb-24"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-24"

        # No clients configured — all clients unknown.
        _write_staged_clients_yaml(tmp_config_dir, "other-client")

        # Session with a client that is NOT in the loaded effective_clients.
        sess = Session(
            id="fb-sess-24",
            name=f"unknown-client/auto-dev/{ticket_id}",
            client="unknown-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="surf-24",
            started_at=started_at,
        )
        context_dir = worktree / ".claude"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "cw-context.json").write_text(
            '{"headless": true, "session_id": "fb-sess-24"}'
        )

        task = TicketTask(
            ticket_id=ticket_id,
            client="unknown-client",
            status=QueueItemStatus.RUNNING,
            session_id="fb-sess-24",
            stage=Stage.FINALIZE,
        )

        state = CwState(sessions=[sess])
        config = _auto_config()
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect._has_commits_beyond_base", lambda _p, _b: True
        )
        pr_calls: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile.stalled._detect.pr_exists_for_branch",
            lambda b, **_kw: pr_calls.append(b) or (False, True),  # type: ignore[func-returns-value]
        )

        candidates = _detect_stalled_candidates(
            state,
            now=now,
            config=config,
            task_by_ticket={ticket_id: task},
        )

        # Unknown client → finalize-blocked detection skipped entirely.
        # Session should still be detected as REVERT_TASK (timed out).
        fb_candidates = [
            c for c in candidates if c.proposed_action.value == "park_finalize_blocked"
        ]
        assert not fb_candidates
        # pr_exists_for_branch must NOT have been called for this unknown client
        # (previously would have been called with the "main" fallback).
        assert not pr_calls

    # ── 1.25 _RESCUE_PR_BODY_TEMPLATE in _shared (NIT 5) ────────────────────

    def test_rescue_pr_body_template_in_shared(self) -> None:
        """_RESCUE_PR_BODY_TEMPLATE is importable from _shared (NIT 5)."""
        from cw.reconcile._shared import (
            _RESCUE_PR_BODY_TEMPLATE,
            _RESCUE_PR_CLOSES_TRAILER_TEMPLATE,
        )

        assert "finalize" in _RESCUE_PR_BODY_TEMPLATE.lower()
        assert "{ticket_id}" in _RESCUE_PR_BODY_TEMPLATE
        assert "Closes #" in _RESCUE_PR_CLOSES_TRAILER_TEMPLATE.format(ticket_id="1293")

    # ── 1.26 _rescue_open_pr Closes trailer wiring (#1293) ──────────────────

    def test_rescue_open_pr_numeric_ticket_includes_closes_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real numeric ticket_id gets a 'Closes #<n>' trailer appended to
        the PR body so the auto-rescued PR auto-closes the linked issue on
        merge (#1293)."""
        from cw.reconcile.salvage import _rescue_open_pr

        run_mock = MagicMock()
        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", run_mock)

        result = _rescue_open_pr("dev/1293", "main", "1293")

        assert result is True
        run_mock.assert_called_once()
        args = run_mock.call_args[0][0]
        body = args[args.index("--body") + 1]
        assert "Closes #1293" in body

    def test_rescue_open_pr_unknown_ticket_id_omits_closes_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A None ticket_id must NOT get a Closes trailer -- falls back to the
        unconditional 'Ticket: #unknown'-style text via the untouched
        _RESCUE_PR_BODY_TEMPLATE (#1293)."""
        from cw.reconcile.salvage import _rescue_open_pr

        run_mock = MagicMock()
        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", run_mock)

        result = _rescue_open_pr("dev/none-ticket", "main", None)

        assert result is True
        run_mock.assert_called_once()
        args = run_mock.call_args[0][0]
        body = args[args.index("--body") + 1]
        assert "Closes #" not in body
        assert "unknown" in body

    def test_rescue_open_pr_nonnumeric_ticket_id_omits_closes_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric (e.g. Linear-style) ticket_id must NOT get a Closes
        trailer -- mirrors the real non-numeric ticket_id case proven live in
        test_rescue_happy_path, and is the exact case the ticket's root-cause
        analysis warns against (#1293)."""
        from cw.reconcile.salvage import _rescue_open_pr

        run_mock = MagicMock()
        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", run_mock)

        result = _rescue_open_pr("dev/fb-10", "main", "FB-10")

        assert result is True
        run_mock.assert_called_once()
        args = run_mock.call_args[0][0]
        body = args[args.index("--body") + 1]
        assert "Closes #" not in body
        assert "Ticket: #FB-10" in body


# ---------------------------------------------------------------------------
# _apply_idle_queue_mutations disposition stamping
# ---------------------------------------------------------------------------
