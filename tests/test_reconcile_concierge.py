"""Tests for cw.reconcile.concierge (RFC 0008 capstone, GitHub #1015)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cw.config import save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
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
from cw.reconcile.concierge import (
    DEFAULT_CONCIERGE_RECOVERIES,
    RECIPE_CANCELLED_ROW_RESTORE,
    RECIPE_FALSE_PARK_REQUEUE,
    RECIPE_PARK_MARKER_POISON_CLEAR,
    resolve_concierge_recipe_enabled,
    run_concierge_recoveries,
)

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


def _write_acme_clients_yaml(tmp_config_dir: Path, workspace: Path) -> None:
    """Write a minimal clients.yaml for 'acme' pointing at *workspace*."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  acme:\n    workspace_path: {workspace}\n"
        "    default_branch: main\n"
    )


def _make_task(
    ticket_id: str = "GEN-1",
    client: str = "acme",
    status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    disposition: str | None = None,
    attempts: int = 1,
    stage: Stage = Stage.PLAN,
    **kwargs: Any,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=status,
        disposition=disposition,
        attempts=attempts,
        stage=stage,
        **kwargs,
    )


def _make_session(
    ticket_id: str = "GEN-1",
    client: str = "acme",
    session_id: str = "sess-1",
    surface_ref: str | None = "surf-1",
    last_result: dict[str, Any] | None = None,
    consecutive_salvage_skips: int = 0,
    started_at: datetime = _NOW,
) -> Session:
    return Session(
        id=session_id,
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        surface_ref=surface_ref,
        last_result=last_result,
        consecutive_salvage_skips=consecutive_salvage_skips,
        started_at=started_at,
    )


@pytest.fixture(autouse=True)
def _flat_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every session's transcript to flat/unreachable (age=None).

    Individual tests override via monkeypatch when they need a specific
    staleness value (recipe 2's per-stage-floor check) or a "still live"
    transcript (recently active — override _transcript_age_seconds to a
    value below TRANSCRIPT_LIVENESS_WINDOW_SECONDS).
    """
    monkeypatch.setattr(
        "cw.reconcile.concierge._transcript_age_seconds", lambda *_a, **_k: None
    )


def _config(**kwargs: Any) -> OrchestratorConfig:
    kwargs.setdefault("concierge_enabled", True)
    return OrchestratorConfig(**kwargs)


class TestConfigGating:
    def test_disabled_is_full_noop(self, tmp_config_dir: Path) -> None:
        """concierge_enabled=False → zero mutation, zero recovered ids."""
        task = _make_task(disposition="stalled_retry_cap_parked")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config(concierge_enabled=False)
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_resolve_recipe_enabled_defaults_true(self) -> None:
        for recipe in DEFAULT_CONCIERGE_RECOVERIES:
            assert (
                resolve_concierge_recipe_enabled(OrchestratorConfig(), recipe) is True
            )

    def test_per_recipe_override_does_not_disable_others(self) -> None:
        """Q7: setting one recipe key must not silently disable the others."""
        cfg = OrchestratorConfig(
            concierge_recoveries={RECIPE_FALSE_PARK_REQUEUE: False}
        )
        assert resolve_concierge_recipe_enabled(cfg, RECIPE_FALSE_PARK_REQUEUE) is False
        assert (
            resolve_concierge_recipe_enabled(cfg, RECIPE_PARK_MARKER_POISON_CLEAR)
            is True
        )
        assert (
            resolve_concierge_recipe_enabled(cfg, RECIPE_CANCELLED_ROW_RESTORE) is True
        )

    def test_disabling_recipe_via_config_skips_it_at_runtime(
        self, tmp_config_dir: Path
    ) -> None:
        task = _make_task(disposition="stalled_retry_cap_parked")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW,
            native_live=set(),
            config=_config(concierge_recoveries={RECIPE_FALSE_PARK_REQUEUE: False}),
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_adr0006_orthogonal_to_reap_policy(self, tmp_config_dir: Path) -> None:
        """reap_policy=SIGNAL_ONLY (default) does not block any recipe."""
        task = _make_task(disposition="stalled_retry_cap_parked", attempts=1)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        cfg = _config(reap_policy=ReapPolicy.SIGNAL_ONLY)
        recovered = run_concierge_recoveries(now=_NOW, native_live=set(), config=cfg)

        assert recovered == ["GEN-1"]


class TestRecipeFalseParkRequeue:
    def test_requeues_stalled_cap_parked_row(self, tmp_config_dir: Path) -> None:
        task = _make_task(disposition="stalled_retry_cap_parked", attempts=2)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.status == QueueItemStatus.PENDING
        assert requeued.disposition is None
        assert requeued.session_id is None
        assert requeued.stage_base_ref is None
        assert requeued.stage == Stage.PLAN  # requeued at CURRENT stage

    def test_requeues_null_disposition_row(self, tmp_config_dir: Path) -> None:
        """A null-disposition BLOCKED_ON_USER row is also eligible."""
        task = _make_task(disposition=None, attempts=1)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]

    @pytest.mark.parametrize(
        "disposition",
        [
            ReapReason.WALL_CLOCK_BUDGET.value,
            ReapReason.IDLE_STALL.value,
            ReapReason.PHANTOM_SURFACE.value,
        ],
    )
    def test_requeues_signal_only_reroute_disposition_row(
        self, tmp_config_dir: Path, disposition: str
    ) -> None:
        """#976: SIGNAL_ONLY reroute dispositions (wall_clock_budget/idle_stall/
        phantom_surface) used to land as disposition=None and were recoverable
        via the None branch — recipe 1 must keep recovering this population now
        that they carry a real disposition, or auto-recovery silently regresses."""
        task = _make_task(disposition=disposition, attempts=1)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]

    def test_requeues_silently_idle_disposition_row_with_no_session_record(
        self, tmp_config_dir: Path
    ) -> None:
        """#976: idle.py's silently-idle park now stamps disposition=
        "silently_idle" instead of None. _has_park_marker's exclusion (recipe
        2's domain) only fires when a session record still exists — a row
        whose session has since been pruned entirely has no marker to check,
        so pre-#976 (disposition=None) it was still recipe-1-eligible. Must
        remain eligible now that it carries a real disposition."""
        task = _make_task(disposition="silently_idle", attempts=1)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]

    def test_non_matching_disposition_untouched(self, tmp_config_dir: Path) -> None:
        """A BLOCKED_ON_USER row with an unrelated disposition is left alone."""
        task = _make_task(disposition="dirty_worktree", attempts=1)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].disposition == "dirty_worktree"

    def test_ceiling_refusal_leaves_row_parked(self, tmp_config_dir: Path) -> None:
        """A1/A2: at the global attempt ceiling, the row is refused, not requeued."""
        task = _make_task(disposition="stalled_retry_cap_parked", attempts=10)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        cfg = _config(global_attempt_ceiling=10)
        recovered = run_concierge_recoveries(now=_NOW, native_live=set(), config=cfg)

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].disposition == "stalled_retry_cap_parked"

    def test_still_live_session_is_skipped(self, tmp_config_dir: Path) -> None:
        """A row whose session's surface_ref IS in native_live is not touched."""
        task = _make_task(disposition="stalled_retry_cap_parked")
        session = _make_session(surface_ref="surf-live")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live={"surf-live"}, config=_config()
        )

        assert recovered == []

    def test_recently_active_transcript_is_skipped(self, tmp_config_dir: Path) -> None:
        """Session absent from roster, but transcript still recently active."""
        task = _make_task(disposition="stalled_retry_cap_parked")
        session = _make_session(surface_ref="surf-dead")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "cw.reconcile.concierge._transcript_age_seconds",
                lambda *_a, **_k: 10.0,  # well under TRANSCRIPT_LIVENESS_WINDOW_SECONDS
            )
            recovered = run_concierge_recoveries(
                now=_NOW, native_live=set(), config=_config()
            )

        assert recovered == []

    def test_event_emitted_before_act_and_survives_failed_act(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CONCIERGE_RECOVERED is durably recorded even if the queue write
        after it raises."""
        from cw.events import read_events

        task = _make_task(disposition="stalled_retry_cap_parked")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        real_save_dev_queue = save_dev_queue
        boom_msg = "simulated write failure"

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError(boom_msg)

        monkeypatch.setattr("cw.reconcile.concierge.save_dev_queue", _boom)

        with pytest.raises(RuntimeError, match=boom_msg):
            run_concierge_recoveries(now=_NOW, native_live=set(), config=_config())

        events = read_events(
            consumer="test-false-park-emit-before-act",
            event_types=[OrchestratorEventType.CONCIERGE_RECOVERED],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["recipe"] == RECIPE_FALSE_PARK_REQUEUE

        # Cleanup: restore real save so the row can still be inspected/left as-is.
        monkeypatch.setattr(
            "cw.reconcile.concierge.save_dev_queue", real_save_dev_queue
        )


class TestDeadOnArrivalBackoff:
    """GitHub #1030: false_park_requeue churn backoff.

    ``dead_on_arrival`` is computed as:
        active_lifespan_seconds = max(0, elapsed_seconds - transcript_age_seconds)
        dead_on_arrival = active_lifespan_seconds < 120.0

    True → the act phase arms exponential backoff (increments
    ``false_park_recovery_count``, stamps
    ``false_park_recovery_next_eligible_at``, emits
    ``CONCIERGE_RECOVERY_BACKOFF_ARMED`` before ``CONCIERGE_RECOVERED``).
    False → the act phase resets both fields to their zero-value. The
    requeue to PENDING always proceeds either way.
    """

    def _dead_session(self, *, elapsed_seconds: float) -> Session:
        return _make_session(
            surface_ref="surf-dead",
            started_at=_NOW - timedelta(seconds=elapsed_seconds),
        )

    def test_below_threshold_lifespan_arms_backoff(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """active_lifespan = 305 - 300 = 5s < 120s → dead_on_arrival True.

        transcript_age_seconds=300.0 is also >= TRANSCRIPT_LIVENESS_WINDOW_SECONDS
        (300), satisfying the flatness gate the row must pass before recipe 1
        even considers it — dead_on_arrival is only ever evaluated on rows
        already proven flat (GitHub #1030: the two checks now share one
        transcript-age lookup, so a candidate's age must be realistic for both).
        """
        monkeypatch.setattr(
            "cw.reconcile.concierge._transcript_age_seconds",
            lambda *_a, **_k: 300.0,
        )
        task = _make_task(disposition="stalled_retry_cap_parked", attempts=1)
        session = self._dead_session(elapsed_seconds=305.0)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.status == QueueItemStatus.PENDING
        assert requeued.false_park_recovery_count == 1
        assert requeued.false_park_recovery_next_eligible_at == _NOW + timedelta(
            seconds=300
        )

    def test_at_or_above_threshold_lifespan_resets_counters(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """active_lifespan = 420 - 300 = 120s, NOT < 120s → dead_on_arrival False.

        transcript_age_seconds=300.0 still satisfies the flatness gate (>=
        TRANSCRIPT_LIVENESS_WINDOW_SECONDS), so this row still reaches the
        dead_on_arrival computation — it's the elapsed/age *difference* that
        crosses the threshold here, not the flatness check.
        """
        monkeypatch.setattr(
            "cw.reconcile.concierge._transcript_age_seconds",
            lambda *_a, **_k: 300.0,
        )
        task = _make_task(
            disposition="stalled_retry_cap_parked",
            attempts=1,
            false_park_recovery_count=3,
            false_park_recovery_next_eligible_at=_NOW - timedelta(seconds=10),
        )
        session = self._dead_session(elapsed_seconds=420.0)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.status == QueueItemStatus.PENDING
        assert requeued.false_park_recovery_count == 0
        assert requeued.false_park_recovery_next_eligible_at is None

    def test_missing_evidence_no_session_is_false(self, tmp_config_dir: Path) -> None:
        """Missing-evidence arm 1: session is None → dead_on_arrival False,
        never armed."""
        task = _make_task(disposition=None, attempts=1)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.false_park_recovery_count == 0
        assert requeued.false_park_recovery_next_eligible_at is None

    def test_missing_evidence_no_transcript_age_is_false(
        self, tmp_config_dir: Path
    ) -> None:
        """Missing-evidence arm 2: _transcript_age_seconds(...) is None (the
        _flat_transcript autouse fixture's default) → dead_on_arrival False,
        never armed, and the formula is never evaluated against a None
        operand."""
        task = _make_task(disposition="stalled_retry_cap_parked", attempts=1)
        session = _make_session(
            surface_ref="surf-dead", started_at=_NOW - timedelta(seconds=200)
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.false_park_recovery_count == 0
        assert requeued.false_park_recovery_next_eligible_at is None

    def test_backoff_armed_event_emitted_before_recovered_event(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CONCIERGE_RECOVERY_BACKOFF_ARMED fires before CONCIERGE_RECOVERED,
        same act-phase pass, for a dead_on_arrival=True candidate."""
        from cw.events import read_events

        monkeypatch.setattr(
            "cw.reconcile.concierge._transcript_age_seconds",
            lambda *_a, **_k: 300.0,
        )
        task = _make_task(disposition="stalled_retry_cap_parked", attempts=1)
        session = self._dead_session(elapsed_seconds=305.0)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        run_concierge_recoveries(now=_NOW, native_live=set(), config=_config())

        events = read_events(
            consumer="test-backoff-armed-ordering",
            event_types=[
                OrchestratorEventType.CONCIERGE_RECOVERY_BACKOFF_ARMED,
                OrchestratorEventType.CONCIERGE_RECOVERED,
            ],
        )
        assert [e.type for e in events] == [
            OrchestratorEventType.CONCIERGE_RECOVERY_BACKOFF_ARMED,
            OrchestratorEventType.CONCIERGE_RECOVERED,
        ]
        armed = events[0]
        assert armed.payload["ticket_id"] == "GEN-1"
        assert armed.payload["client"] == "acme"
        assert armed.payload["recovery_count"] == 1
        assert armed.payload["next_eligible_at"] is not None
        assert armed.payload["session_id"] == session.id

    def test_no_backoff_armed_event_when_dead_on_arrival_false(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.events import read_events

        monkeypatch.setattr(
            "cw.reconcile.concierge._transcript_age_seconds",
            lambda *_a, **_k: 300.0,
        )
        task = _make_task(disposition="stalled_retry_cap_parked", attempts=1)
        session = self._dead_session(elapsed_seconds=420.0)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        run_concierge_recoveries(now=_NOW, native_live=set(), config=_config())

        events = read_events(
            consumer="test-no-backoff-armed",
            event_types=[OrchestratorEventType.CONCIERGE_RECOVERY_BACKOFF_ARMED],
        )
        assert events == []

    def test_deferral_skip_while_next_eligible_at_in_future(
        self, tmp_config_dir: Path
    ) -> None:
        """A row still inside its backoff window is not even considered this
        cycle — it stays BLOCKED_ON_USER, no candidate is generated."""
        task = _make_task(
            disposition="stalled_retry_cap_parked",
            attempts=1,
            false_park_recovery_count=1,
            false_park_recovery_next_eligible_at=_NOW + timedelta(seconds=60),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].false_park_recovery_count == 1

    def test_deferral_window_elapsed_generates_candidate_again(
        self, tmp_config_dir: Path
    ) -> None:
        """Once now >= next_eligible_at, the row is eligible again."""
        task = _make_task(
            disposition="stalled_retry_cap_parked",
            attempts=1,
            false_park_recovery_count=1,
            false_park_recovery_next_eligible_at=_NOW - timedelta(seconds=1),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]

    @pytest.mark.parametrize(
        ("count", "expected_delay"),
        [
            (1, 300.0),
            (2, 600.0),
            (3, 1200.0),
            (4, 2400.0),
            (5, 3600.0),  # 300 * 2**4 = 4800, capped at 3600
            (10, 3600.0),  # far beyond the cap, still held at 3600
        ],
    )
    def test_backoff_delay_doubles_then_caps(
        self, count: int, expected_delay: float
    ) -> None:
        """Direct unit coverage of the doubling formula and its cap — the
        act-phase integration tests above only ever exercise count=1's
        unscaled initial delay, which would not catch an exponent or cap bug
        at higher counts."""
        from cw.reconcile.concierge import _resolve_false_park_recovery_backoff

        assert _resolve_false_park_recovery_backoff(count) == expected_delay

    def test_second_consecutive_dead_on_arrival_doubles_next_eligible_at(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: a row already at false_park_recovery_count=1 (armed by
        a prior cycle) that dies dead-on-arrival AGAIN gets the doubled delay
        (600s, not another unscaled 300s) — proving the count-dependent
        formula is actually wired through run_concierge_recoveries, not just
        correct in isolation."""
        monkeypatch.setattr(
            "cw.reconcile.concierge._transcript_age_seconds",
            lambda *_a, **_k: 300.0,
        )
        task = _make_task(
            disposition="stalled_retry_cap_parked",
            attempts=1,
            false_park_recovery_count=1,
            false_park_recovery_next_eligible_at=_NOW - timedelta(seconds=1),
        )
        session = self._dead_session(elapsed_seconds=305.0)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        requeued = load_dev_queue().tasks[0]
        assert requeued.false_park_recovery_count == 2
        assert requeued.false_park_recovery_next_eligible_at == _NOW + timedelta(
            seconds=600
        )


class TestRecipeParkMarkerPoisonClear:
    def _stale_45m(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.reconcile.concierge._transcript_age_seconds",
            lambda *_a, **_k: 46 * 60,
        )

    def test_clears_park_marker_when_dead_and_stale(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=1, session_id="sess-1")
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
            surface_ref="surf-dead",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.status == QueueItemStatus.PENDING
        assert requeued.session_id is None
        state = CwState.model_validate_json(
            (tmp_config_dir / ".local" / "share" / "cw" / "sessions.json").read_text()
        )
        closed = state.find_by_name_or_id("sess-1")
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED

    def test_clears_park_marker_when_dead_and_stale_silently_idle_disposition(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#976: recipe 1's newly-added `_SILENTLY_IDLE_REASON` eligibility
        must not steal a marker-bearing session from recipe 2 — _has_park_marker
        reads only session.last_result, never task.disposition, so a row whose
        session still carries the marker stays recipe 2's domain regardless of
        which disposition string is now in recipe 1's frozenset."""
        self._stale_45m(monkeypatch)
        task = _make_task(disposition="silently_idle", attempts=1, session_id="sess-1")
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
            surface_ref="surf-dead",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        state = CwState.model_validate_json(
            (tmp_config_dir / ".local" / "share" / "cw" / "sessions.json").read_text()
        )
        closed = state.find_by_name_or_id("sess-1")
        assert closed is not None
        # recipe 2's act (session closed), not recipe 1's:
        assert closed.status == SessionStatus.COMPLETED

    def test_cycling_threshold_zero_skips_count(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Q4: consecutive_salvage_skips == 0 is NOT eligible."""
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=1)
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=0,
            surface_ref="surf-dead",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []

    def test_still_live_session_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=1)
        session = _make_session(
            last_result={"paused_status": "needs_salvage"},
            consecutive_salvage_skips=2,
            surface_ref="surf-live",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live={"surf-live"}, config=_config()
        )

        assert recovered == []

    def test_not_yet_stale_45m_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-stage-floor check via _classify_liveness_bucket: below 45m, skip."""
        monkeypatch.setattr(
            "cw.reconcile.concierge._transcript_age_seconds",
            lambda *_a, **_k: 20 * 60,
        )
        task = _make_task(disposition=None, attempts=1, stage=Stage.PLAN)
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
            surface_ref="surf-dead",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []

    def test_ceiling_gate_a2(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A2: recipe 2 also gates on attempts < global_attempt_ceiling."""
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=10)
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
            surface_ref="surf-dead",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config(global_attempt_ceiling=10)
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER


class TestRecipeCancelledRowRestore:
    def test_restores_cancelled_row_with_commits_ahead(
        self, tmp_config_dir: Path, make_git_repo: Any
    ) -> None:
        repo = make_git_repo("acme")
        (repo / "origin").mkdir()
        # Simulate a remote by cloning the repo state via a bare-ish local
        # "origin/main" ref: fast-forward a fake remote ref to HEAD, then add
        # one more commit so HEAD is ahead of origin/main.
        import subprocess

        clean_subprocess_run = subprocess.run

        def _git(*args: str) -> None:
            clean_subprocess_run(
                ["git", "-C", str(repo), *args], capture_output=True, check=True
            )

        _git("update-ref", "refs/remotes/origin/main", "HEAD")
        (repo / "file.txt").write_text("hello")
        _git("add", "file.txt")
        _git("commit", "-m", "feature work")

        _write_acme_clients_yaml(tmp_config_dir, repo)

        task = _make_task(
            status=QueueItemStatus.CANCELLED,
            disposition=None,
            attempts=3,
            worktree_path=repo,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        restored = store.tasks[0]
        assert restored.status == QueueItemStatus.PENDING
        assert restored.attempts == 3  # A3: no attempts increment on restore

    def test_no_worktree_row_untouched(self, tmp_config_dir: Path) -> None:
        task = _make_task(
            status=QueueItemStatus.CANCELLED, disposition=None, worktree_path=None
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.CANCELLED

    def test_zero_commits_row_untouched(
        self, tmp_config_dir: Path, make_git_repo: Any
    ) -> None:
        repo = make_git_repo("acme")
        import subprocess

        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"],
            capture_output=True,
            check=True,
        )
        _write_acme_clients_yaml(tmp_config_dir, repo)

        task = _make_task(
            status=QueueItemStatus.CANCELLED, disposition=None, worktree_path=repo
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []


class TestComboEntryPoint:
    def test_returns_combined_recovered_ids_across_recipes(
        self, tmp_config_dir: Path, make_git_repo: Any
    ) -> None:
        false_park = _make_task(
            ticket_id="GEN-1", disposition="stalled_retry_cap_parked", attempts=1
        )
        no_touch = _make_task(
            ticket_id="GEN-2", disposition="dirty_worktree", attempts=1
        )
        save_dev_queue(DevQueueStore(tasks=[false_park, no_touch]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]

    def test_no_op_when_dev_queue_empty(self, tmp_config_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []
