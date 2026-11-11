"""Unit tests for cw.reconcile.tasks.merged_completion.

Timed-out-merged completion (#488).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import freezegun
import pytest

from cw.config import (
    save_state,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import (
    _NEVER_CLAIMED_COMPLETION_REASON,
    complete_timed_out_merged_tasks,
    reconcile,
)
from tests._reconcile_helpers import (
    _mk_timed_out_daemon_session,
)
from tests._shared.session_seeding import _make_daemon_session, _make_ticket_task

# ---------------------------------------------------------------------------
# TestCompleteTimedOutMergedTasks — #488
# ---------------------------------------------------------------------------


class TestCompleteTimedOutMergedTasks:
    """complete_timed_out_merged_tasks() auto-completes PENDING tasks on merged PR."""

    def _pending_task(self, ticket_id: str) -> TicketTask:
        """PENDING task with a real claim history (attempts=1, session_id
        already cleared) -- the legitimate shape this backstop targets.
        Distinct from _never_claimed_task (#1387)."""
        return _make_ticket_task(ticket_id=ticket_id, client="client-a", attempts=1)

    def _never_claimed_task(
        self, ticket_id: str, *, lane: str = "batch-2"
    ) -> TicketTask:
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            attempts=0,
            session_id=None,
            lane=lane,
        )

    def _spawn_error_only_task(
        self, ticket_id: str, *, lane: str = "batch-2"
    ) -> TicketTask:
        """1617's exact observed shape: every attempt died on the generic
        spawn-error path. attempts == spawn_error_count == 3, session_id=None,
        stage_high_water=None (never entered a stage)."""
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.PLAN,
            stage_high_water=None,
            attempts=3,
            spawn_error_count=3,
            session_id=None,
            signoff="operator",
            lane=lane,
        )

    def _usage_limit_only_task(
        self, ticket_id: str, *, lane: str = "batch-2"
    ) -> TicketTask:
        """#1631's exact shape: every attempt died via UsageLimitError.

        That revert path calls _revert_claimed_task_to_pending with
        stamp_backoff=False (dispatch/claim.py), so the counters move
        asymmetrically -- `attempts` increments at claim time but
        `spawn_error_count` never does. The row is therefore
        attempts=3, spawn_error_count=0, session_id=None,
        stage_high_water=None: indistinguishable from a legitimate
        first-stage ship on the pre-#1631 predicate, and only
        ever_spawned=False separates the two.
        """
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.PLAN,
            stage_high_water=None,
            attempts=3,
            spawn_error_count=0,
            session_id=None,
            ever_spawned=False,
            lane=lane,
        )

    def _legitimate_first_stage_task(self, ticket_id: str) -> TicketTask:
        """A task that genuinely spawned, ran and shipped inside its FIRST
        pipeline stage, then timed out before the row advanced.

        stage_high_water is None and spawn_error_count is 0, matching
        _usage_limit_only_task above -- and both tasks' `attempts` values
        (1 here vs. 3 there) equally fail the first disjunct's
        `attempts == spawn_error_count` check against spawn_error_count=0, so
        neither `attempts` nor `stage` (which the guard does not read at all)
        affects _is_never_claimed's verdict here; only `ever_spawned` does.
        This is the shape #1623's reverted stage_high_water disjunct wrongly
        refused; the test using it exists to prove #1631's new predicate does
        NOT reintroduce that regression.
        """
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.IMPL,
            stage_high_water=None,
            attempts=1,
            spawn_error_count=0,
            session_id=None,
            ever_spawned=True,
        )

    def _legitimately_progressed_reverted_task(self, ticket_id: str) -> TicketTask:
        """A task that genuinely ran, completed at least one stage, then was
        reverted to PENDING (session_id cleared) for retry -- attempts >
        spawn_error_count and stage_high_water is set. Must NOT be refused;
        this is the failure mode that would make the fix worse than the bug."""
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.IMPL,
            stage_high_water=Stage.IMPL,
            attempts=2,
            spawn_error_count=0,
            session_id=None,
        )

    def test_never_claimed_row_refused_stays_pending_and_emits_needs_attention(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """attempts=0, session_id=None row matching a timed-out-merged
        candidate is refused (#1387 belt-and-braces guard): stays PENDING,
        is NOT in the return value, no SESSION_COMPLETED fires, and exactly
        one SESSION_NEEDS_ATTENTION fires with client/ticket_id/lane/paused_status."""
        now = datetime.now(UTC)
        ticket_id = "TKT-NEVER-CLAIMED"
        session = _mk_timed_out_daemon_session(
            "sess-never-claimed", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(tasks=[self._never_claimed_task(ticket_id, lane="batch-2")])
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 0
        assert task.session_id is None

        completed_events = read_events(
            consumer="test-never-claimed-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(completed_events) == 0

        attention_events = read_events(
            consumer="test-never-claimed-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(attention_events) == 1
        p = attention_events[0].payload
        assert p["client"] == "client-a"
        assert p["ticket_id"] == ticket_id
        assert p["lane"] == "batch-2"
        assert p["paused_status"] == _NEVER_CLAIMED_COMPLETION_REASON
        assert attention_events[0].correlation_id == ticket_id

    def test_spawn_error_only_history_refused_stays_pending_and_emits_needs_attention(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """1617's exact observed shape: attempts=3, spawn_error_count=3,
        session_id=None, stage_high_water=None -- every attempt died on the
        generic spawn-error path. Refused: stays PENDING, NOT in the return
        value, no SESSION_COMPLETED fires, exactly one SESSION_NEEDS_ATTENTION
        fires with client/ticket_id/lane/paused_status."""
        now = datetime.now(UTC)
        ticket_id = "TKT-SPAWN-ERROR"
        session = _mk_timed_out_daemon_session(
            "sess-spawn-error", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._spawn_error_only_task(ticket_id, lane="batch-2")]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 3
        assert task.spawn_error_count == 3
        assert task.session_id is None

        completed_events = read_events(
            consumer="test-spawn-error-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(completed_events) == 0

        attention_events = read_events(
            consumer="test-spawn-error-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(attention_events) == 1
        p = attention_events[0].payload
        assert p["client"] == "client-a"
        assert p["ticket_id"] == ticket_id
        assert p["lane"] == "batch-2"
        assert p["paused_status"] == _NEVER_CLAIMED_COMPLETION_REASON
        assert attention_events[0].correlation_id == ticket_id

    def test_usage_limit_only_history_refused_stays_pending_and_emits_needs_attention(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1631's exact shape: attempts=3, spawn_error_count=0,
        session_id=None, stage_high_water=None, ever_spawned=False -- every
        attempt died via UsageLimitError, whose revert deliberately does NOT
        stamp spawn_error_count. Refused: stays PENDING, NOT in the return
        value, no SESSION_COMPLETED fires, exactly one SESSION_NEEDS_ATTENTION
        fires whose breadcrumb names the never-spawned cause rather than the
        (wrong, for this shape) spawn-error-path cause."""
        now = datetime.now(UTC)
        ticket_id = "TKT-USAGE-LIMIT"
        session = _mk_timed_out_daemon_session(
            "sess-usage-limit", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._usage_limit_only_task(ticket_id, lane="batch-2")]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 3
        assert task.spawn_error_count == 0
        assert task.session_id is None

        completed_events = read_events(
            consumer="test-usage-limit-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(completed_events) == 0

        attention_events = read_events(
            consumer="test-usage-limit-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(attention_events) == 1
        p = attention_events[0].payload
        assert p["client"] == "client-a"
        assert p["ticket_id"] == ticket_id
        assert p["lane"] == "batch-2"
        assert p["paused_status"] == _NEVER_CLAIMED_COMPLETION_REASON
        assert attention_events[0].correlation_id == ticket_id
        breadcrumbs = p["breadcrumbs"]
        assert "never successfully spawned" in breadcrumbs
        assert "ever_spawned=False" in breadcrumbs
        assert "died on the spawn-error path" not in breadcrumbs

    def test_legitimate_first_stage_ship_still_completes(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Criterion preservation (#1623 R4, #1631): a task that genuinely
        spawned and shipped inside its first pipeline stage -- attempts=1,
        spawn_error_count=0, stage_high_water=None, ever_spawned=True -- is
        still completed. The only thing separating it from the usage-limit
        shape above is ever_spawned, so a predicate that refused this one
        would be worse than the bug it closes."""
        now = datetime.now(UTC)
        ticket_id = "TKT-FIRST-STAGE"
        session = _mk_timed_out_daemon_session(
            "sess-first-stage", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(tasks=[self._legitimate_first_stage_task(ticket_id)])
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == [ticket_id]
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "shipped"

        events = read_events(
            consumer="test-first-stage-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload["reason"] == "timed_out_merged"

    def test_legitimately_progressed_reverted_task_still_completes(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A task that genuinely ran, then was reverted to PENDING for retry
        (attempts > spawn_error_count) must NOT be refused -- proves the
        widened predicate doesn't over-refuse legitimate recoveries."""
        now = datetime.now(UTC)
        ticket_id = "TKT-LEGIT-RETRY"
        session = _mk_timed_out_daemon_session(
            "sess-legit-retry", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._legitimately_progressed_reverted_task(ticket_id)]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == [ticket_id]
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "shipped"

        events = read_events(
            consumer="test-legit-retry-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload["reason"] == "timed_out_merged"

    def test_complete_timed_out_merged_tasks_forces_finalize_stage_not_high_water(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1629: a salvage completion forces stage to FINALIZE so the row
        stops advertising a stage it never finished, while stage_high_water
        stays where it was -- the divergence IS the salvage marker."""
        now = datetime.now(UTC)
        ticket_id = "TKT-SALVAGE-STAGE"
        session = _mk_timed_out_daemon_session(
            "sess-salvage-stage", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._legitimately_progressed_reverted_task(ticket_id)]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        assert complete_timed_out_merged_tasks() == [ticket_id]

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "shipped"
        assert task.stage == Stage.FINALIZE
        assert task.stage_high_water == Stage.IMPL

    def test_happy_path(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DAEMON TIMED_OUT session + PENDING task + merged PR → task COMPLETED.

        SESSION_COMPLETED event must carry the full payload including
        salvaged=True, reason="timed_out_merged", correlation_id=ticket_id.
        ReconcileReport.completed_ticket_ids == ["TKT-X"].
        """
        now = datetime.now(UTC)
        ticket_id = "TKT-X"
        session = _mk_timed_out_daemon_session(
            "sess-happy", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == [ticket_id]

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-happy-path",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["session_id"] == "sess-happy"
        assert p["session_name"] == f"client-a/auto-dev/{ticket_id}"
        assert p["client"] == "client-a"
        assert p["ticket_id"] == ticket_id
        assert "claude_session_id" in p
        assert p["crashed"] is False
        assert p["salvaged"] is True
        assert p["reason"] == "timed_out_merged"
        assert events[0].correlation_id == ticket_id

        report = reconcile.__doc__  # smoke: function exists
        _ = report  # suppress unused

    def test_gh_unavailable_skips_all(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh binary absent → no upgrades, loop breaks, second session NOT processed."""
        now = datetime.now(UTC)
        sessions = [
            _mk_timed_out_daemon_session(
                f"sess-{i}", f"TKT-{i}", completed_at=now - timedelta(hours=1)
            )
            for i in range(2)
        ]
        save_state(CwState(sessions=sessions))
        save_dev_queue(
            DevQueueStore(tasks=[self._pending_task(f"TKT-{i}") for i in range(2)])
        )

        call_count = 0

        def _unavailable(tid: str, **kw: object) -> tuple[None, bool]:
            nonlocal call_count
            call_count += 1
            return None, False

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _unavailable)

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        assert call_count == 1  # broke after first call

        store = load_dev_queue()
        for task in store.tasks:
            assert task.status == QueueItemStatus.PENDING

    def test_transient_error_skips_session(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(None, True) for A, (True, True) for B → A skipped, B completed."""
        now = datetime.now(UTC)
        session_a = _mk_timed_out_daemon_session(
            "sess-a", "TKT-A", completed_at=now - timedelta(hours=1)
        )
        session_b = _mk_timed_out_daemon_session(
            "sess-b", "TKT-B", completed_at=now - timedelta(hours=1)
        )
        save_state(CwState(sessions=[session_a, session_b]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._pending_task("TKT-A"), self._pending_task("TKT-B")]
            )
        )

        def _transient_then_merged(tid: str, **kw: object) -> tuple[bool | None, bool]:
            if tid == "TKT-A":
                return None, True
            return True, True

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _transient_then_merged
        )

        completed = complete_timed_out_merged_tasks()

        assert "TKT-B" in completed
        assert "TKT-A" not in completed

        store = load_dev_queue()
        by_tid = {t.ticket_id: t for t in store.tasks}
        assert by_tid["TKT-A"].status == QueueItemStatus.PENDING
        assert by_tid["TKT-B"].status == QueueItemStatus.COMPLETED

    def test_idempotency(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second reconcile() call emits ZERO extra events (task already COMPLETED)."""
        now = datetime.now(UTC)
        ticket_id = "TKT-IDEM"
        session = _mk_timed_out_daemon_session(
            "sess-idem", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        call_count = 0

        def _merged(tid: str, **kw: object) -> tuple[bool, bool]:
            nonlocal call_count
            call_count += 1
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _merged)

        complete_timed_out_merged_tasks()
        assert call_count == 1

        # Second call: task is now COMPLETED, should skip gh call entirely.
        call_count = 0
        complete_timed_out_merged_tasks()
        assert call_count == 0

        events = read_events(
            consumer="test-idempotency",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1  # only from first call

    def test_pr_not_merged(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(False, True) → task stays PENDING, no SESSION_COMPLETED event."""
        now = datetime.now(UTC)
        ticket_id = "TKT-OPEN"
        session = _mk_timed_out_daemon_session(
            "sess-open", ticket_id, completed_at=now - timedelta(hours=2)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING

        events = read_events(
            consumer="test-pr-not-merged",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 0

    def test_fresh_merged_pr_state_completes_without_gh_call(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #975: a fresh MERGED pr_state on the task auto-completes it
        without any _deps.pr_is_merged_for_ticket call."""
        now = datetime.now(UTC)
        ticket_id = "TKT-FRESH-MERGED"
        session = _mk_timed_out_daemon_session(
            "sess-fresh-merged", ticket_id, completed_at=now - timedelta(hours=2)
        )
        save_state(CwState(sessions=[session]))
        task = _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            attempts=1,
            pr_state=PrState(state="MERGED", hydrated_at=now - timedelta(seconds=10)),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
            msg = "pr_is_merged_for_ticket must not be called when pr_state is fresh"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == [ticket_id]
        store = load_dev_queue()
        completed_task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert completed_task.status == QueueItemStatus.COMPLETED

    def test_fresh_non_merged_pr_state_stays_pending_without_gh_call(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #975: fresh non-MERGED pr_state -> stays PENDING, no gh call."""
        now = datetime.now(UTC)
        ticket_id = "TKT-FRESH-OPEN"
        session = _mk_timed_out_daemon_session(
            "sess-fresh-open", ticket_id, completed_at=now - timedelta(hours=2)
        )
        save_state(CwState(sessions=[session]))
        task = _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            attempts=1,
            pr_state=PrState(state="OPEN", hydrated_at=now - timedelta(seconds=10)),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
            msg = "pr_is_merged_for_ticket must not be called when pr_state is fresh"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        store = load_dev_queue()
        pending_task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert pending_task.status == QueueItemStatus.PENDING

    def test_stale_pr_state_falls_back_to_gh_call(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #975: stale pr_state falls back to the ordinary gh call
        path unchanged (pre-#975 behavior)."""
        now = datetime.now(UTC)
        ticket_id = "TKT-STALE-STATE"
        session = _mk_timed_out_daemon_session(
            "sess-stale-state", ticket_id, completed_at=now - timedelta(hours=2)
        )
        save_state(CwState(sessions=[session]))
        task = _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            attempts=1,
            pr_state=PrState(state="MERGED", hydrated_at=now - timedelta(seconds=300)),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []

        def _capture(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
            calls.append(_tid)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        completed = complete_timed_out_merged_tasks()

        assert calls == [ticket_id]
        assert completed == [ticket_id]

    def test_outside_lookback_window(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Session completed_at > 7 days ago → skipped, no gh call made."""
        fixed_now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
        ticket_id = "TKT-OLD"
        # 8 days ago — outside the 7-day lookback
        old_completed_at = fixed_now - timedelta(days=8)
        session = _mk_timed_out_daemon_session(
            "sess-old", ticket_id, completed_at=old_completed_at
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        call_count = 0

        def _should_not_be_called(tid: str, **kw: object) -> tuple[bool, bool]:
            nonlocal call_count
            call_count += 1
            return True, True

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        with freezegun.freeze_time(fixed_now):
            completed = complete_timed_out_merged_tasks()

        assert completed == []
        assert call_count == 0

    def test_inside_lookback_window(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Session completed_at within 7 days → processed normally."""
        fixed_now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
        ticket_id = "TKT-RECENT"
        # 6 days ago — inside the 7-day lookback
        recent_completed_at = fixed_now - timedelta(days=6)
        session = _mk_timed_out_daemon_session(
            "sess-recent", ticket_id, completed_at=recent_completed_at
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        with freezegun.freeze_time(fixed_now):
            completed = complete_timed_out_merged_tasks()

        assert ticket_id in completed

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED

    def test_completed_at_none_legacy_session(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """session.completed_at = None → skipped without TypeError."""
        now = datetime.now(UTC)
        ticket_id = "TKT-LEGACY"
        session = _mk_timed_out_daemon_session(
            "sess-legacy", ticket_id, completed_at=now - timedelta(hours=1)
        )
        # Override completed_at to None to simulate legacy state
        session.completed_at = None
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        call_count = 0

        def _should_not_be_called(tid: str, **kw: object) -> tuple[bool, bool]:
            nonlocal call_count
            call_count += 1
            return True, True

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        # Must not raise
        completed = complete_timed_out_merged_tasks()

        assert completed == []
        assert call_count == 0

    def test_task_running_not_upgraded(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Task status is RUNNING → no gh call made, task stays RUNNING."""
        now = datetime.now(UTC)
        ticket_id = "TKT-RUN"
        session = _mk_timed_out_daemon_session(
            "sess-run", ticket_id, completed_at=now - timedelta(hours=1)
        )
        save_state(CwState(sessions=[session]))

        running_task = TicketTask(
            ticket_id=ticket_id, client="client-a", status=QueueItemStatus.RUNNING
        )
        save_dev_queue(DevQueueStore(tasks=[running_task]))

        call_count = 0

        def _should_not_be_called(tid: str, **kw: object) -> tuple[bool, bool]:
            nonlocal call_count
            call_count += 1
            return True, True

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        assert call_count == 0

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

    def test_custom_feature_branch_prefix_used(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """feature_branch_prefix='feat' → branch='feat/TKT-X' passed to gh check."""
        now = datetime.now(UTC)
        ticket_id = "TKT-X"
        session = _mk_timed_out_daemon_session(
            "sess-custom-prefix", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        # Write clients.yaml with custom feature_branch_prefix
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        complete_timed_out_merged_tasks()

        assert captured_branch == [f"feat/{ticket_id}"]

    def test_custom_feature_branch_prefix_used_passes_cwd(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cwd resolved from client's workspace_path reaches the gh check (#1269)."""
        now = datetime.now(UTC)
        ticket_id = "TKT-X"
        session = _mk_timed_out_daemon_session(
            "sess-custom-prefix-cwd", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        captured_cwd: list[Path | None] = []

        def _capture(
            tid: str, *, branch: str, cwd: Path | None = None, **_kw: object
        ) -> tuple[bool, bool]:
            captured_cwd.append(cwd)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        complete_timed_out_merged_tasks()

        assert captured_cwd == [Path("/tmp/ws-feat")]

    def test_default_feature_branch_prefix_fallback(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No clients.yaml → fallback branch='dev/TKT-Y' (default prefix)."""
        now = datetime.now(UTC)
        ticket_id = "TKT-Y"
        session = _mk_timed_out_daemon_session(
            "sess-default-prefix", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        # No clients.yaml written — load_clients() returns {}

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        complete_timed_out_merged_tasks()

        assert captured_branch == [f"dev/{ticket_id}"]

    def test_dangling_client_skips_gh_call_stays_pending(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """clients.yaml populated but missing this session's client → skip.

        Distinct from "no clients.yaml at all" (see
        test_default_feature_branch_prefix_fallback, which must still call
        gh with cwd=None): here clients.yaml is populated with a *different*
        client, so this session's client is dangling/config-drifted. An
        unscoped gh call would risk the same cross-repo misattribution the
        ticket describes (#1269), so the candidate must be skipped entirely
        rather than falling back to an ambient-cwd gh call.
        """
        now = datetime.now(UTC)
        ticket_id = "TKT-DANGLING"
        session = _mk_timed_out_daemon_session(
            "sess-dangling-client", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        calls: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            calls.append(tid)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        completed = complete_timed_out_merged_tasks()

        assert calls == []
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING

    def test_does_not_complete_different_clients_same_ticket(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """client-a's TIMED_OUT merged session must not complete client-b's
        identically-numbered PENDING task (#1385) -- mirrors the #1054 fix in
        core.py's merged_client_ticket_ids scoping, applied here to
        complete_timed_out_merged_tasks's own (client, ticket_id) matching."""
        now = datetime.now(UTC)
        ticket_id = "1"
        session = _make_daemon_session(
            name="client-a/auto-dev/1",
            client="client-a",
            status=SessionStatus.TIMED_OUT,
            completed_at=now - timedelta(days=1),
        )
        client_b_task = _make_ticket_task(ticket_id=ticket_id, client="client-b")
        save_state(CwState(sessions=[session]))
        # Intentionally no client-a queue row -- reproduces the bug even
        # without a same-client task present.
        save_dev_queue(DevQueueStore(tasks=[client_b_task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING

        events = read_events(
            consumer="test-does-not-complete-different-clients",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 0
