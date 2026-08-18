"""Tests for cw.reconcile.concierge (RFC 0008 capstone, GitHub #1015)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    DEFAULT_LANE,
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
from cw.reconcile.concierge import (
    DEFAULT_CONCIERGE_RECOVERIES,
    RECIPE_CANCELLED_ROW_RESTORE,
    RECIPE_FALSE_PARK_REQUEUE,
    RECIPE_PARK_MARKER_POISON_CLEAR,
    resolve_concierge_recipe_enabled,
    run_concierge_recoveries,
)
from tests._reconcile_helpers import (
    SCOPE_GUARD_FILES,
    SCOPE_GUARD_LINES,
    _inflate_scope,
    _make_stale_base_repo,
    _make_terminal_payload,
    _shipped_salvage_payload,
    _write_salvage_transcript,
)
from tests.conftest import _make_daemon_session, _make_ticket_task

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


def _write_acme_clients_yaml(tmp_config_dir: Path, workspace: Path) -> None:
    """Write a minimal clients.yaml for 'acme' pointing at *workspace*."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  acme:\n    workspace_path: {workspace}\n"
        "    default_branch: main\n"
    )


def _write_acme_clients_yaml_with_lane(
    tmp_config_dir: Path,
    workspace: Path,
    *,
    lane_name: str,
    attempt_ceiling: str,
) -> None:
    """Write an 'acme' clients.yaml whose *lane_name* carries *attempt_ceiling*.

    Deliberately separate from :func:`_write_acme_clients_yaml` (#1751) so the
    lane-less default every other test in this file relies on stays untouched
    — those tests are themselves the regression guard proving the concierge
    still falls through to the global ceiling when no lane opts in.
    *attempt_ceiling* is the raw YAML token (``"false"``, ``"25"``) so a test
    can assert the disable state and an override value are genuinely distinct.
    """
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  acme:\n    workspace_path: {workspace}\n"
        "    default_branch: main\n"
        "    lanes:\n"
        f"      - name: {lane_name}\n"
        f"        attempt_ceiling: {attempt_ceiling}\n"
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
    return _make_ticket_task(
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
    worktree_path: Path | None = None,
    **kwargs: Any,
) -> Session:
    return _make_daemon_session(
        id=session_id,
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        surface_ref=surface_ref,
        worktree_path=worktree_path,
        last_result=last_result,
        consecutive_salvage_skips=consecutive_salvage_skips,
        started_at=started_at,
        **kwargs,
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

    def test_review_health_gate_disposition_untouched(
        self, tmp_config_dir: Path
    ) -> None:
        """#1702: a dead-session row parked ``review_health_gate`` is NOT
        auto-recovered by the false-park requeue recipe.

        Auto-recovering it would misclassify a real review-coverage finding as
        a technical/session-death glitch, defeating the gate's purpose.
        """
        task = _make_task(disposition="review_health_gate", attempts=1)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].disposition == "review_health_gate"

    def test_must_fix_mechanically_rejected_disposition_untouched(
        self, tmp_config_dir: Path
    ) -> None:
        """#1714: a dead-session row parked ``codex_must_fix_mechanically_rejected``
        is NOT auto-recovered by the false-park requeue recipe.

        Mirror of ``test_review_health_gate_disposition_untouched``: auto-
        recovering it would misclassify a dropped MUST_FIX finding as a
        technical/session-death glitch, defeating the gate's purpose.
        """
        task = _make_task(
            disposition="codex_must_fix_mechanically_rejected", attempts=1
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].disposition == "codex_must_fix_mechanically_rejected"

    def test_branch_staleness_park_is_not_false_park_eligible(
        self, tmp_config_dir: Path
    ) -> None:
        """#1823: a dead-session row parked ``branch_behind_main`` is NOT
        auto-recovered by the false-park requeue recipe.

        Third member of the same family as ``review_health_gate`` (#1702) and
        ``codex_must_fix_mechanically_rejected`` (#1714): auto-requeuing it
        would spin a genuinely stale branch back through the pipeline without
        an operator ever rebasing it, defeating the gate.
        """
        from cw.dev_queue import BRANCH_STALENESS_GATE_DISPOSITION
        from cw.reconcile.concierge import _FALSE_PARK_ELIGIBLE_DISPOSITIONS

        assert BRANCH_STALENESS_GATE_DISPOSITION not in (
            _FALSE_PARK_ELIGIBLE_DISPOSITIONS
        )

        task = _make_task(disposition=BRANCH_STALENESS_GATE_DISPOSITION, attempts=1)
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].disposition == "branch_behind_main"

    def test_ceiling_refusal_leaves_row_parked(self, tmp_config_dir: Path) -> None:
        """A1/A2: at the global attempt ceiling, the row is refused, not requeued."""
        # #1750: refused_ceiling reads unproductive_attempts, not attempts.
        task = _make_task(
            disposition="stalled_retry_cap_parked",
            attempts=10,
            unproductive_attempts=10,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        cfg = _config(global_attempt_ceiling=10)
        recovered = run_concierge_recoveries(now=_NOW, native_live=set(), config=cfg)

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].disposition == "stalled_retry_cap_parked"

    def test_refused_ceiling_reads_unproductive_attempts_not_attempts(
        self, tmp_config_dir: Path
    ) -> None:
        """#1750 recipe 1: a productive row at high raw attempts is NOT refused.

        The concierge must agree with the dispatch claim path about which
        counter the ceiling reads, or it would refuse a requeue that
        _claim_next_pending would happily have allowed.
        """
        task = _make_task(
            disposition="stalled_retry_cap_parked", attempts=10, unproductive_attempts=1
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        cfg = _config(global_attempt_ceiling=10)
        recovered = run_concierge_recoveries(now=_NOW, native_live=set(), config=cfg)

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.PENDING

    def test_lane_ceiling_override_changes_refused_ceiling(
        self, tmp_config_dir: Path
    ) -> None:
        """#1751 recipe 1: a lane that disables the ceiling is not refused.

        The row sits at the *global* ceiling, which before #1751 was the only
        number this recipe could read. Its lane disables the cap, and the
        dispatch claim path would happily re-claim it — so refusing here would
        reintroduce exactly the claim/concierge drift #1750's comment warns
        against, one layer up.
        """
        _write_acme_clients_yaml_with_lane(
            tmp_config_dir,
            tmp_config_dir / "ws",
            lane_name=DEFAULT_LANE,
            attempt_ceiling="false",
        )
        task = _make_task(
            disposition="stalled_retry_cap_parked",
            attempts=10,
            unproductive_attempts=10,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        cfg = _config(global_attempt_ceiling=10)
        recovered = run_concierge_recoveries(now=_NOW, native_live=set(), config=cfg)

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.PENDING

    def test_unknown_client_falls_through_to_global_ceiling(
        self, tmp_config_dir: Path
    ) -> None:
        """#1751 recipe 1: no clients.yaml → CwError → global ceiling still applies.

        This is the state every other test in this file runs in, so it is also
        the guard that the lane lookup did not change their behaviour.
        """
        task = _make_task(
            disposition="stalled_retry_cap_parked",
            attempts=10,
            unproductive_attempts=10,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        cfg = _config(global_attempt_ceiling=10)
        recovered = run_concierge_recoveries(now=_NOW, native_live=set(), config=cfg)

        assert recovered == []
        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

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


class TestHookContextConflictRefusal:
    """GitHub #1674: recipe 1 refuses a requeue it already proved is futile.

    A DAEMON session that died under ``ReapPolicy.SIGNAL_ONLY`` keeps
    ``Session.status`` non-terminal forever, so its ``cw-context.json`` blocks
    every worktree reuse. ``task.hook_context_conflict_session_id`` records the
    session that produced the ``HookContextConflictError``; when the row's
    currently-resolved session IS that session and it is still non-terminal,
    requeuing can only burn another attempt, so the act phase leaves the row
    parked and emits ``CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED``.

    The refusal is scoped to session *identity* plus non-terminal status, so it
    clears on its own once the operator runs ``cw spawn close --confirmed-dead
    <id>`` (status goes terminal) or once a new session supersedes the old one.
    """

    def test_repeat_conflict_against_same_session_refuses_requeue(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.events import read_events

        task = _make_task(
            disposition=ReapReason.PHANTOM_SURFACE.value,
            attempts=3,
            hook_context_conflict_session_id="sess-1",
        )
        session = _make_session(session_id="sess-1", surface_ref="surf-dead")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == []
        parked = load_dev_queue().tasks[0]
        assert parked.status == QueueItemStatus.BLOCKED_ON_USER
        assert parked.disposition == ReapReason.PHANTOM_SURFACE.value
        assert parked.attempts == 3

        events = read_events(
            consumer="test-hook-context-conflict-refused",
            event_types=[
                OrchestratorEventType.CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED,
                OrchestratorEventType.CONCIERGE_RECOVERED,
            ],
        )
        assert [e.type for e in events] == [
            OrchestratorEventType.CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED
        ]
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["client"] == "acme"
        assert events[0].payload["recipe"] == RECIPE_FALSE_PARK_REQUEUE
        assert events[0].payload["session_id"] == "sess-1"

    def test_first_time_conflict_field_unset_still_requeues(
        self, tmp_config_dir: Path
    ) -> None:
        """Regression guard: the ordinary crashed-session case is unaffected.

        A roster-dead session whose status was never flipped is recipe 1's
        primary intended catch — refusing on status alone would break the
        recipe's whole purpose.
        """
        task = _make_task(
            disposition=ReapReason.PHANTOM_SURFACE.value,
            attempts=1,
            hook_context_conflict_session_id=None,
        )
        session = _make_session(session_id="sess-1", surface_ref="surf-dead")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]

    def test_conflict_against_a_different_session_id_still_requeues(
        self, tmp_config_dir: Path
    ) -> None:
        """The refusal is scoped to the SAME session, not "ever conflicted"."""
        task = _make_task(
            disposition=ReapReason.PHANTOM_SURFACE.value,
            attempts=1,
            hook_context_conflict_session_id="sess-OLD",
        )
        session = _make_session(session_id="sess-1", surface_ref="surf-dead")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]

    @pytest.mark.parametrize(
        "closed_status", [SessionStatus.COMPLETED, SessionStatus.TIMED_OUT]
    )
    def test_terminal_conflicting_session_clears_the_refusal(
        self, tmp_config_dir: Path, closed_status: SessionStatus
    ) -> None:
        """``cw spawn close --confirmed-dead`` flips status, not id — the
        status clause is what lets the refusal clear on the next cycle."""
        task = _make_task(
            disposition=ReapReason.PHANTOM_SURFACE.value,
            attempts=1,
            hook_context_conflict_session_id="sess-1",
        )
        session = _make_session(
            session_id="sess-1", surface_ref="surf-dead", status=closed_status
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]

    def test_refusal_and_ceiling_refusal_do_not_double_emit(
        self, tmp_config_dir: Path
    ) -> None:
        """Both refusals true at once: one non-mutation outcome, no crash.

        Precedence is irrelevant because both arms ``continue`` — the ceiling
        check runs first and emits nothing, so exactly zero events are
        recorded and the row stays parked.
        """
        from cw.events import read_events

        task = _make_task(
            disposition=ReapReason.PHANTOM_SURFACE.value,
            attempts=10,
            unproductive_attempts=10,  # #1750: the counter refused_ceiling reads
            hook_context_conflict_session_id="sess-1",
        )
        session = _make_session(session_id="sess-1", surface_ref="surf-dead")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config(global_attempt_ceiling=10)
        )

        assert recovered == []
        parked = load_dev_queue().tasks[0]
        assert parked.status == QueueItemStatus.BLOCKED_ON_USER
        assert parked.disposition == ReapReason.PHANTOM_SURFACE.value

        events = read_events(
            consumer="test-hook-context-conflict-double-emit",
            event_types=[
                OrchestratorEventType.CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED,
                OrchestratorEventType.CONCIERGE_RECOVERED,
            ],
        )
        assert events == []

    def test_session_not_found_with_no_prior_conflict_predicate_is_false(
        self, tmp_config_dir: Path
    ) -> None:
        """Fail-closed guard: ``session is None`` + field at its ``None``
        default must never satisfy the identity check.

        A predicate written as ``task.hook_context_conflict_session_id ==
        (session.id if session else None)`` evaluates ``None == None`` here and
        would wrongly refuse an ordinary row that never had a conflict. None of
        the other cases in this class construct that combination.
        """
        from cw.reconcile.concierge import _detect_false_park_candidates

        task = _make_task(
            disposition=ReapReason.PHANTOM_SURFACE.value,
            attempts=1,
            hook_context_conflict_session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        candidates = _detect_false_park_candidates(
            CwState(sessions=[]),
            [task],
            now=_NOW,
            native_live=set(),
            config=_config(),
        )
        assert [c.refused_hook_context_conflict for c in candidates] == [False]

    def test_session_not_found_with_no_prior_conflict_still_requeues_normally(
        self, tmp_config_dir: Path
    ) -> None:
        """Companion to the predicate-level test above: the same session-None
        + field-unset combination must also requeue normally through the full
        recovery pipeline, not just at the detect-phase predicate."""
        task = _make_task(
            disposition=ReapReason.PHANTOM_SURFACE.value,
            attempts=1,
            hook_context_conflict_session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]


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
        # #1750: refused_ceiling reads unproductive_attempts, not attempts.
        task = _make_task(disposition=None, attempts=10, unproductive_attempts=10)
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

    def test_lane_ceiling_override_changes_refused_ceiling(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1751 recipe 2: a lane that disables the ceiling is not refused.

        Mirror of recipe 1's counterpart — both recipes must route through the
        same lane-scoped resolver as the dispatch claim path.
        """
        self._stale_45m(monkeypatch)
        _write_acme_clients_yaml_with_lane(
            tmp_config_dir,
            tmp_config_dir / "ws",
            lane_name=DEFAULT_LANE,
            attempt_ceiling="false",
        )
        task = _make_task(disposition=None, attempts=10, unproductive_attempts=10)
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

        assert recovered == ["GEN-1"]

    def test_ceiling_gate_a2_reads_unproductive_attempts_not_attempts(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1750 recipe 2: high raw attempts alone no longer gates the recipe."""
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=10, unproductive_attempts=1)
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

        assert recovered == ["GEN-1"]

    def test_salvages_terminal_sentinel_instead_of_crashing_blocked_on_user_status(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1353 defect (b): recipe 2 must attempt salvage before
        stamping CRASHED, mirroring idle.py/stalled.py/phantom.py's own
        pre-close salvage check. A recovered review_pending_approval sentinel
        routes the task to BLOCKED_ON_USER (not PENDING) and the session to
        COMPLETED/NORMAL (not CRASHED)."""
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=1, session_id="sess-1")
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
            surface_ref="surf-dead",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        fake_result = AutoDevResult.model_validate(
            _make_terminal_payload("review_pending_approval", "GEN-1")
        )
        monkeypatch.setattr(
            "cw.reconcile.concierge.salvage_terminal_result",
            lambda *_a, **_kw: (fake_result, "fake-claude-id"),
        )

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.status == QueueItemStatus.BLOCKED_ON_USER
        assert requeued.disposition == "review_pending_approval"
        state = CwState.model_validate_json(
            (tmp_config_dir / ".local" / "share" / "cw" / "sessions.json").read_text()
        )
        closed = state.find_by_name_or_id("sess-1")
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.NORMAL
        assert closed.last_result is not None
        assert closed.last_result["status"] == "review_pending_approval"

    def test_salvages_terminal_sentinel_instead_of_crashing_completed_status(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same shape as above but with a 'shipped' salvage result — task lands
        COMPLETED with disposition 'shipped', session completed_reason NORMAL."""
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=1, session_id="sess-1")
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
            surface_ref="surf-dead",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        fake_result = AutoDevResult.model_validate(_shipped_salvage_payload())
        monkeypatch.setattr(
            "cw.reconcile.concierge.salvage_terminal_result",
            lambda *_a, **_kw: (fake_result, "fake-claude-id"),
        )

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.status == QueueItemStatus.COMPLETED
        assert requeued.disposition == "shipped"
        state = CwState.model_validate_json(
            (tmp_config_dir / ".local" / "share" / "cw" / "sessions.json").read_text()
        )
        closed = state.find_by_name_or_id("sess-1")
        assert closed is not None
        assert closed.completed_reason == CompletionReason.NORMAL

    def test_no_salvage_result_preserves_existing_crash_behavior(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: when salvage finds nothing, the pre-existing
        crash-stamp path is untouched — matches
        test_clears_park_marker_when_dead_and_stale's assertions exactly."""
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=1, session_id="sess-1")
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
            surface_ref="surf-dead",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        monkeypatch.setattr(
            "cw.reconcile.concierge.salvage_terminal_result",
            lambda *_a, **_kw: None,
        )

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
        assert closed.completed_reason == CompletionReason.CRASHED

    def test_salvage_check_skipped_for_non_daemon_session_origin(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The salvage gate checks session.origin is SessionOrigin.DAEMON before
        ever calling salvage_terminal_result — a USER-origin session is still
        crash-stamped even when the (mocked) salvage lookup would hit,
        mirroring phantom.py's own DAEMON-only salvage gate."""
        self._stale_45m(monkeypatch)
        task = _make_task(disposition=None, attempts=1, session_id="sess-1")
        session = _make_session(
            last_result={"paused_status": "silently_idle"},
            consecutive_salvage_skips=1,
            surface_ref="surf-dead",
            origin=SessionOrigin.USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[session]))

        fake_result = AutoDevResult.model_validate(
            _make_terminal_payload("review_pending_approval", "GEN-1")
        )
        monkeypatch.setattr(
            "cw.reconcile.concierge.salvage_terminal_result",
            lambda *_a, **_kw: (fake_result, "fake-claude-id"),
        )

        recovered = run_concierge_recoveries(
            now=_NOW, native_live=set(), config=_config()
        )

        assert recovered == ["GEN-1"]
        store = load_dev_queue()
        requeued = store.tasks[0]
        assert requeued.status == QueueItemStatus.PENDING
        state = CwState.model_validate_json(
            (tmp_config_dir / ".local" / "share" / "cw" / "sessions.json").read_text()
        )
        closed = state.find_by_name_or_id("sess-1")
        assert closed is not None
        assert closed.completed_reason == CompletionReason.CRASHED


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


def test_park_marker_poison_clear_survives_widened_transcript_lookup(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1283 SHOULD_FIX: fix (b)'s widened mtime lookup must not regress recipe 2.

    A stale registered transcript plus a moderately-fresher-but-still->45m-old
    sibling subagent transcript still classifies STALE_45M, so the park-marker
    poison-clear still fires. Locks that the widening doesn't drop a genuinely
    dead worker below the 45m staleness threshold and suppress the clear.
    """
    import os

    from cw.reconcile._shared import _transcript_age_seconds as _real_age
    from cw.reconcile.concierge import _park_marker_transcript_stale_45m
    from tests.conftest import _write_idle_transcript

    # The autouse _flat_transcript fixture stubs age to None; restore the real
    # (widened) implementation so this test exercises the glob across all files.
    monkeypatch.setattr("cw.reconcile.concierge._transcript_age_seconds", _real_age)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-poison-widen"
    started_at = _NOW - timedelta(hours=3)

    # Registered transcript (surface_ref-prefixed), long stale.
    reg = _write_idle_transcript(home, worktree, filename="fake-short-id-sess.jsonl")
    reg_ts = (_NOW - timedelta(minutes=90)).timestamp()
    os.utime(str(reg), (reg_ts, reg_ts))
    # Sibling subagent transcript: fresher, but still older than 45 minutes.
    sib = _write_idle_transcript(home, worktree, filename="subagent-mid.jsonl")
    sib_ts = (_NOW - timedelta(minutes=50)).timestamp()
    os.utime(str(sib), (sib_ts, sib_ts))

    session = _make_session(
        last_result={"paused_status": "needs_salvage"},
        surface_ref="fake-short-id",
        started_at=started_at,
    )
    session.worktree_path = worktree
    task = _make_task(disposition=None, attempts=1, stage=Stage.IMPL)

    assert (
        _park_marker_transcript_stale_45m(session, task, now=_NOW, config=_config())
        is True
    )


class TestParkMarkerPoisonClearDoorRefusal:
    """RFC 0012 A3 (#1459): recipe 2's routing when the door refuses the salvage.

    _close_confirmed_dead_session now returns a refusal EmitOutcome when another
    authority already holds a terminal result. These tests drive
    _act_on_park_marker_poison_candidates directly with a fabricated refusal so
    each routing arm is exercised in isolation (round-3 4-branch set).
    """

    def _candidate(self) -> Any:
        from cw.reconcile.concierge import ConciergeCandidate

        return ConciergeCandidate(
            ticket_id="GEN-1",
            client="acme",
            recipe=RECIPE_PARK_MARKER_POISON_CLEAR,
            evidence={},
            session_id="sess-1",
            refused_ceiling=False,
        )

    def _refusal(self, existing_result: dict[str, Any]) -> Any:
        from cw.models import LastResultSource
        from cw.result import EmitOutcome

        return EmitOutcome(
            session_id="sess-1",
            result=None,
            prior_status=existing_result.get("status"),
            refused=True,
            existing_result=existing_result,
            existing_source=LastResultSource.STOP_HOOK_HARVEST,
        )

    def _run(self, monkeypatch: pytest.MonkeyPatch, refusal: Any) -> TicketTask:
        from cw.reconcile.concierge import _act_on_park_marker_poison_candidates

        task = _make_task(disposition=None, attempts=1, session_id="sess-1")
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))
        monkeypatch.setattr(
            "cw.reconcile.concierge._close_confirmed_dead_session",
            lambda *_a, **_kw: (False, None, refusal),
        )
        recovered = _act_on_park_marker_poison_candidates([self._candidate()], now=_NOW)
        assert recovered == ["GEN-1"]
        return load_dev_queue().tasks[0]

    def test_routes_routable_foreign_autodev_result(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A validating non-blocked AutoDevResult routes via
        _queue_status_for_salvaged's default (shipped -> COMPLETED), not PENDING."""
        refusal = self._refusal(_shipped_salvage_payload())
        requeued = self._run(monkeypatch, refusal)
        assert requeued.status == QueueItemStatus.COMPLETED
        assert requeued.disposition == "shipped"

    def test_routes_blocked_status_autodev_result_to_blocked_on_user(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A validating AutoDevResult whose status=='blocked' (carries
        schema_version) routes to BLOCKED_ON_USER via the concierge special case,
        NOT to COMPLETED via _queue_status_for_salvaged's default (round-3 bug)."""
        blocked_autodev = _make_terminal_payload("review_pending_approval", "GEN-1")
        blocked_autodev["status"] = "blocked"
        blocked_autodev["pr"] = None
        blocked_autodev["next_actions"] = []
        blocked_autodev["blocker"] = {
            "stage": "s2",
            "reason": "impl_failed",
            "details": "x",
        }
        refusal = self._refusal(blocked_autodev)
        requeued = self._run(monkeypatch, refusal)
        assert requeued.status == QueueItemStatus.BLOCKED_ON_USER
        # Regression pin (#1254): a non-hold blocker reason keeps the verbatim
        # status disposition -- _hold_aware_disposition is a strict superset.
        assert requeued.disposition == "blocked"

    def test_routes_blocked_result_shape_to_blocked_on_user(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A validating BlockedResult (status=='blocked', no schema_version)
        routes to BLOCKED_ON_USER via the isinstance(BlockedResult) branch."""
        blocked_result = {
            "status": "blocked",
            "blocker": {"stage": "s1", "reason": "validation_failed", "details": "x"},
        }
        refusal = self._refusal(blocked_result)
        requeued = self._run(monkeypatch, refusal)
        assert requeued.status == QueueItemStatus.BLOCKED_ON_USER
        # Regression pin (#1254): see sibling above.
        assert requeued.disposition == "blocked"

    def test_routes_refused_operator_unavailable_blocked_result(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RFC 0011 A1 (#1254): the door-refused foreign-result arm reads
        blocker.reason, so a push_auth_failed BlockedResult refusal parks with the
        hold-class disposition instead of the verbatim "blocked"."""
        blocked_result = {
            "status": "blocked",
            "blocker": {"stage": "s4", "reason": "push_auth_failed", "details": "x"},
        }
        refusal = self._refusal(blocked_result)
        requeued = self._run(monkeypatch, refusal)
        assert requeued.status == QueueItemStatus.BLOCKED_ON_USER
        assert requeued.disposition == "awaiting_operator"

    def test_unroutable_foreign_shape_falls_through_to_pending(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An existing_result that fails BOTH validations falls through to the
        PENDING-requeue floor, logging a warning naming the source and shape."""
        # {"status": "blocked"} with no blocker, no schema_version -> discriminant
        # picks BlockedResult but validation fails on the missing blocker field.
        refusal = self._refusal({"status": "blocked"})
        with caplog.at_level("WARNING"):
            requeued = self._run(monkeypatch, refusal)
        assert requeued.status == QueueItemStatus.PENDING
        assert requeued.session_id is None
        assert requeued.stage_base_ref is None
        assert any(
            "unroutable" in r.getMessage() and "stop_hook_harvest" in r.getMessage()
            for r in caplog.records
        )


def test_park_marker_poison_salvage_operator_unavailable_stamps_awaiting_operator(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFC 0011 A1 (#1254): recipe 2's *salvage* arm (door accepted a fresh
    salvage) also reads blocker.reason. merge_gate_blocked may optionally carry a
    blocker per schema.py's #777 exception, so operator_unavailable on that status
    reaches the hold namespace. The queue status itself stays whatever
    _queue_status_for_salvaged maps merge_gate_blocked to -- unchanged here."""
    from cw.reconcile.concierge import (
        ConciergeCandidate,
        _act_on_park_marker_poison_candidates,
    )

    task = _make_task(disposition=None, attempts=1, session_id="sess-1")
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))

    payload = _make_terminal_payload("merge_gate_blocked", "GEN-1")
    payload["blocker"] = {
        "stage": "stage4a_merge_gate",
        "reason": "operator_unavailable",
        "details": "x",
    }
    salvage_result = AutoDevResult.model_validate(payload)
    monkeypatch.setattr(
        "cw.reconcile.concierge._close_confirmed_dead_session",
        lambda *_a, **_kw: (True, salvage_result, None),
    )

    candidate = ConciergeCandidate(
        ticket_id="GEN-1",
        client="acme",
        recipe=RECIPE_PARK_MARKER_POISON_CLEAR,
        evidence={},
        session_id="sess-1",
        refused_ceiling=False,
    )
    recovered = _act_on_park_marker_poison_candidates([candidate], now=_NOW)

    assert recovered == ["GEN-1"]
    requeued = load_dev_queue().tasks[0]
    assert requeued.disposition == "awaiting_operator"


def test_close_confirmed_dead_session_returns_refusal_outcome_on_door_refusal(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFC 0012 A3 (#1459): when the door refuses the pre-close salvage (the
    session already holds a terminal result), _close_confirmed_dead_session
    returns (False, None, refusal) and leaves the session byte-identical."""
    from cw.models import LastResultSource
    from cw.reconcile.concierge import _close_confirmed_dead_session

    foreign = {"status": "shipped", "foreign_authority": True}
    session = _make_session(
        session_id="sess-refuse",
        last_result=foreign,
        last_result_source=LastResultSource.STOP_HOOK_HARVEST,
        surface_ref="surf-dead",
    )
    save_state(CwState(sessions=[session]))
    salvaged = AutoDevResult.model_validate(_shipped_salvage_payload())
    monkeypatch.setattr(
        "cw.reconcile.concierge.salvage_terminal_result",
        lambda *_a, **_kw: (salvaged, "fake-claude-id"),
    )

    changed, salvage_result, refusal = _close_confirmed_dead_session(
        "sess-refuse", _NOW
    )

    assert changed is False
    assert salvage_result is None
    assert refusal is not None
    assert refusal.refused is True
    assert refusal.existing_result == foreign
    assert refusal.existing_source == LastResultSource.STOP_HOOK_HARVEST
    # Session left byte-identical (still ACTIVE, foreign result + source intact).
    state = CwState.model_validate_json(
        (tmp_config_dir / ".local" / "share" / "cw" / "sessions.json").read_text()
    )
    reloaded = state.find_by_name_or_id("sess-refuse")
    assert reloaded is not None
    assert reloaded.status == SessionStatus.ACTIVE
    assert reloaded.last_result == foreign
    assert reloaded.last_result_source == LastResultSource.STOP_HOOK_HARVEST


def test_validate_existing_result_for_routing_none_returns_none() -> None:
    """RFC 0012 A3 (#1459): a None existing_result is trivially unroutable."""
    from cw.reconcile._shared import _validate_existing_result_for_routing

    assert _validate_existing_result_for_routing(None) is None


def test_validate_existing_result_for_routing_invalid_shape_returns_none() -> None:
    """An existing_result that fails discriminated validation returns None."""
    from cw.reconcile._shared import _validate_existing_result_for_routing

    # status=="blocked", no schema_version -> BlockedResult branch, but missing
    # the required blocker field -> EmitValidationError -> None.
    assert _validate_existing_result_for_routing({"status": "blocked"}) is None


def test_close_confirmed_dead_session_corrects_salvaged_scope(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_git_repo: Any,
) -> None:
    """#1487: the concierge's pre-close salvage returns git-verified scope numbers."""
    from cw.reconcile.concierge import _close_confirmed_dead_session

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = _make_stale_base_repo(make_git_repo, "wt-concierge-scope")
    _write_acme_clients_yaml(tmp_config_dir, worktree)

    session = _make_session(
        session_id="sess-scope",
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    save_state(CwState(sessions=[session]))
    _write_salvage_transcript(
        home,
        worktree,
        "claude-uuid-concierge",
        _inflate_scope(_shipped_salvage_payload()),
    )

    changed, salvage_result, refusal = _close_confirmed_dead_session("sess-scope", _NOW)

    assert changed is True
    assert refusal is None
    assert salvage_result is not None
    assert salvage_result.scope.files == SCOPE_GUARD_FILES
    assert salvage_result.scope.lines_actual == SCOPE_GUARD_LINES
