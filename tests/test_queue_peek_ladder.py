"""Tests for ``cw.queue_peek.ladder`` -- the WAIT / PEEK / STOP ladder."""

from __future__ import annotations

from cw import queue_peek
from cw.models import Stage

# ---------------------------------------------------------------------------
# recommend / peek-stop ladder
# ---------------------------------------------------------------------------


class TestRecommend:
    def test_no_transcript_returns_peek(self) -> None:
        rec, reason = queue_peek.recommend(None, None, None, None, 1)
        assert rec == "PEEK"
        assert "no transcript" in reason

    def test_merged_pr_idle_returns_stop(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_POST_PR_MIN + 1.0,
            pr_state="MERGED",
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP"
        assert "merged" in reason.lower()

    def test_merged_pr_not_idle_returns_wait(self) -> None:
        """PR merged but worker still active → falls through to stall check."""
        rec, _ = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state="MERGED",
            sentinel_status=None,
            unproductive_attempts=1,
        )
        # idle_min = 0.0, not > IDLE_POST_PR_MIN, so doesn't STOP — falls to stall check
        assert rec == "WAIT"

    def test_shipped_open_pr_healthy_returns_wait(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=2.0,
            pr_state="OPEN",
            sentinel_status="shipped",
            unproductive_attempts=1,
        )
        assert rec == "WAIT"
        assert "CI" in reason

    def test_shipped_open_pr_stalled_returns_peek(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=20.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state="OPEN",
            sentinel_status="shipped",
            unproductive_attempts=1,
        )
        assert rec == "PEEK"
        assert "CI" in reason

    def test_age_above_stop_threshold_returns_stop(self) -> None:
        # idle_min load-bearing for the liveness gate (#2044) — must be
        # stale, not "demonstrably writing".
        rec, reason = queue_peek.recommend(
            age_min=queue_peek.STOP_AGE_MIN + 1.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP"
        assert "timeout" in reason
        assert "approaches" in reason

    def test_age_exceeds_ceiling_returns_stop_with_exceeded_wording(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=128.7,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP"
        assert "exceeded" in reason
        assert "approaches" not in reason

    def test_age_exactly_at_ceiling_returns_exceeded_wording(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=60.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP"
        assert "exceeded" in reason

    def test_age_just_below_ceiling_returns_approaching_wording(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=59.9,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP"
        assert "approaches" in reason

    def test_long_idle_no_pr_stays_stop_not_suppressed_by_wording_fix(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=128.7,
            idle_min=128.7,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP"

    def test_high_raw_attempts_low_unproductive_attempts_does_not_stop(self) -> None:
        """#1727 shape: a ticket can accumulate many raw claims (attempts)
        while making real progress on each one — the ladder's STOP-by-
        attempt-count branch must key off unproductive_attempts, not raw
        attempts, so an all-productive run is never advised STOP here."""
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=0,
        )
        assert rec != "STOP"
        assert "systemic" not in reason

    def test_high_unproductive_attempts_returns_stop(self) -> None:
        # idle_min load-bearing for the liveness gate (#2044) — must be
        # stale, not "demonstrably writing".
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
        )
        assert rec == "STOP"
        assert "systemic" in reason

    def test_long_idle_no_pr_returns_stop_or_peek(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP-OR-PEEK"

    def test_moderate_idle_no_pr_returns_peek(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_PEEK_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "PEEK"

    def test_mature_age_returns_peek(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=queue_peek.PEEK_AGE_MIN + 1.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "PEEK"

    def test_moderate_age_returns_wait(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=queue_peek.WAIT_AGE_MIN + 1.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "WAIT"

    def test_early_age_returns_wait(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=5.0,
            idle_min=None,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "WAIT"
        assert "early" in reason

    def test_high_attempts_with_usage_limit_signal_does_not_stop(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
            usage_limit_detected=True,
        )
        assert rec != "STOP"
        assert "usage-limit" in reason

    def test_high_attempts_without_usage_limit_signal_still_stops(self) -> None:
        """Regression guard for test_high_unproductive_attempts_returns_stop:
        omitting/False usage_limit_detected preserves the existing STOP
        behavior. idle_min load-bearing for the liveness gate (#2044)."""
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
            usage_limit_detected=False,
        )
        assert rec == "STOP"
        assert "systemic" in reason

    def test_high_attempts_with_child_active_does_not_stop(self) -> None:
        """#2028 comment 4, Instance 2 (session da8f1b20): age_m 5.1, idle_m
        1.3, unproductive attempt 3 — STOP fired via the attempt-count arm
        even though idle_m was barely elevated, because that arm never
        consulted child activity. child_active=True must suppress it and
        fall through to _stall_check instead."""
        rec, reason = queue_peek.recommend(
            age_min=5.1,
            idle_min=1.3,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=3,
            child_active=True,
        )
        assert rec != "STOP"
        assert "systemic" not in reason

    def test_high_attempts_with_child_active_instance_3_does_not_stop(self) -> None:
        """#2028 comment 4, Instance 3 (session 7176f757): age_m 14.2,
        idle_m 9.0, unproductive attempt 4 — same arm, same gap."""
        rec, reason = queue_peek.recommend(
            age_min=14.2,
            idle_min=9.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=4,
            child_active=True,
        )
        assert rec != "STOP"
        assert "systemic" not in reason

    def test_high_attempts_without_child_active_still_stops(self) -> None:
        """Regression guard: omitting/False child_active preserves the
        existing STOP-by-attempt-count behavior — a genuinely wedged session
        with no active child must still be flagged. idle_min load-bearing
        for the liveness gate (#2044)."""
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
            child_active=False,
        )
        assert rec == "STOP"
        assert "systemic" in reason


class TestReachedDeepStage:
    """_reached_deep_stage — pipeline-order gate for stage_high_water (#1361)."""

    def test_review_is_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.REVIEW) is True

    def test_finalize_is_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.FINALIZE) is True

    def test_impl_is_not_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.IMPL) is False

    def test_plan_is_not_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.PLAN) is False

    def test_harden_is_not_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.HARDEN) is False

    def test_none_is_not_deep(self) -> None:
        assert queue_peek._reached_deep_stage(None) is False

    def test_stage_order_is_pipeline_order_not_lexicographic(self) -> None:
        """_STAGE_ORDER must be pipeline order (HARDEN..FINALIZE), NOT the
        lexicographic order plain StrEnum '<' comparison would give — this
        documents the exact trap a naive '<'/'>' comparison would create."""
        order = queue_peek._STAGE_ORDER
        assert (
            order.index(Stage.HARDEN)
            < order.index(Stage.PLAN)
            < order.index(Stage.IMPL)
            < order.index(Stage.REVIEW)
            < order.index(Stage.FINALIZE)
        )
        # Plain StrEnum '<' is lexicographic on the string values and gives
        # the opposite (wrong) answer for this pair.
        assert (Stage.FINALIZE < Stage.HARDEN) is True


class TestScoreSessionStageHighWater:
    """_score_session's attempt-count STOP branch gated on stage_high_water
    (#1361): a ticket that demonstrably reached REVIEW or later falls
    through to the stall-check ladder instead of hard-stopping."""

    def test_high_attempts_with_deep_high_water_falls_through_not_stop(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
            stage_high_water=Stage.REVIEW,
        )
        assert rec != "STOP"
        assert "systemic" not in reason

    def test_high_attempts_with_shallow_high_water_still_stops(self) -> None:
        # idle_min load-bearing for the liveness gate (#2044).
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
            stage_high_water=Stage.IMPL,
        )
        assert rec == "STOP"
        assert "systemic" in reason

    def test_high_attempts_with_none_high_water_still_stops(self) -> None:
        # idle_min load-bearing for the liveness gate (#2044).
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
            stage_high_water=None,
        )
        assert rec == "STOP"
        assert "systemic" in reason

    def test_merged_pr_stuck_stops_even_with_finalize_high_water(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_POST_PR_MIN + 1.0,
            pr_state="MERGED",
            sentinel_status=None,
            unproductive_attempts=1,
            stage_high_water=Stage.FINALIZE,
        )
        assert rec == "STOP"
        assert "merged" in reason.lower()

    def test_age_timeout_stops_even_with_finalize_high_water(self) -> None:
        # idle_min load-bearing for the liveness gate (#2044).
        rec, reason = queue_peek.recommend(
            age_min=queue_peek.STOP_AGE_MIN + 1.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
            stage_high_water=Stage.FINALIZE,
        )
        assert rec == "STOP"
        assert "timeout" in reason

    def test_grinding_but_idle_stalled_falls_through_to_nonwait(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
            stage_high_water=Stage.REVIEW,
        )
        assert rec == "STOP-OR-PEEK"

    def test_high_attempts_usage_limit_and_shallow_stage_falls_through(self) -> None:
        """The new usage_limit_detected flag and the existing stage_high_water
        gate don't double-negate or crash when both independently apply."""
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
            stage_high_water=Stage.IMPL,
            usage_limit_detected=True,
        )
        assert rec != "STOP"
        assert "systemic" not in reason


class TestLivenessGate:
    """_gate_stop_on_liveness — a single verdict-level check applied to every
    STOP-flavored recommendation, regardless of which _score_session arm
    proposed it (#2044)."""

    def test_high_attempts_live_idle_does_not_stop(self) -> None:
        """#2044 primary example (7c159163): idle_min=0.0, no child_active,
        unproductive-attempts arm fires — must be downgraded."""
        rec, reason = queue_peek.recommend(
            age_min=24.9,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=5,
        )
        assert rec != "STOP"
        assert "unproductive attempt" in reason
        assert "indicates active writing" in reason

    def test_high_attempts_stale_idle_still_stops(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=24.9,
            idle_min=20.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=5,
        )
        assert rec == "STOP"
        assert "systemic" in reason

    def test_age_arm_live_idle_does_not_stop(self) -> None:
        """#2044 second example (bf947d61): age arm fires, idle_min=0.1 —
        must be downgraded even though the age arm never reads idle_min
        itself."""
        rec, _ = queue_peek.recommend(
            age_min=66.9,
            idle_min=0.1,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=5,
        )
        assert rec != "STOP"

    def test_age_arm_stale_idle_still_stops(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=66.9,
            idle_min=20.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=5,
        )
        assert rec == "STOP"
        assert "timeout" in reason

    def test_idle_none_does_not_suppress_stop(self) -> None:
        """idle_min is None (no signal) — mirrors _reached_deep_stage's
        'unknown = no signal; does NOT suppress STOP' convention."""
        rec, _ = queue_peek.recommend(
            age_min=queue_peek.STOP_AGE_MIN + 1.0,
            idle_min=None,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP"

    def test_idle_exactly_at_threshold_is_live(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=queue_peek.STOP_AGE_MIN + 1.0,
            idle_min=queue_peek.IDLE_LIVE_MAX_MIN,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec != "STOP"

    def test_idle_just_above_threshold_is_stale(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=queue_peek.STOP_AGE_MIN + 1.0,
            idle_min=queue_peek.IDLE_LIVE_MAX_MIN + 0.1,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == "STOP"

    def test_gate_does_not_touch_non_stop_verdicts(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=5.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec in {"WAIT", "PEEK"}
        assert "indicates active writing" not in reason

    def test_gate_leaves_stall_check_stop_or_peek_unaffected(self) -> None:
        """_stall_check's STOP-OR-PEEK path already requires
        idle_min > IDLE_STALL_MIN (15) to fire — well above
        IDLE_LIVE_MAX_MIN (2.0), so the new gate is a no-op here."""
        age_min = 10.0
        idle_min = queue_peek.IDLE_STALL_MIN + 1.0
        pr_state = None
        direct_rec, direct_reason = queue_peek._stall_check(age_min, idle_min, pr_state)
        rec, reason = queue_peek.recommend(
            age_min=age_min,
            idle_min=idle_min,
            pr_state=pr_state,
            sentinel_status=None,
            unproductive_attempts=1,
        )
        assert rec == direct_rec == "STOP-OR-PEEK"
        assert reason == direct_reason
