"""Dependency-hold core rules (GitHub #2321): slot predicate, sentinel field,
routing stamp, and the serve-tick release/latch behaviour."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cw.auto_dev_result import Blocker, parse_depends_on_pr
from cw.dev_queue import DEPENDENCY_HOLD_DISPOSITION, load_dev_queue, save_dev_queue
from cw.dispatch.routing.stage_failure import (
    _dependency_park_pr,
    _stamp_blocked_on_pr,
)
from cw.models import (
    DevQueueStore,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    TicketTask,
    WatchedPr,
)
from cw.models.tasks.store import occupies_lane_slot
from cw.reconcile.tasks import release_stale_gated_tasks
from tests._ticket_view_helpers import events_of, make_task, queue_row, seed_queue

_HOLD = DEPENDENCY_HOLD_DISPOSITION


def _hold(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": _HOLD,
        "blocked_reason": "dependency_unmerged",
        "blocked_on_pr": 42,
    }
    kwargs.update(overrides)
    return make_task(**kwargs)


class TestSlotPredicate:
    def test_dependency_hold_with_pr_id_frees_its_slot(self) -> None:
        assert occupies_lane_slot(_hold()) is False

    def test_dependency_hold_without_pr_id_keeps_its_slot(self) -> None:
        assert occupies_lane_slot(_hold(blocked_on_pr=None)) is True

    def test_dependency_hold_outside_blocked_on_user_keeps_its_slot(self) -> None:
        running = _hold(status=QueueItemStatus.RUNNING)
        assert occupies_lane_slot(running) is True

    def test_merge_gate_blocked_with_pr_id_still_holds_its_slot(self) -> None:
        gated = _hold(
            disposition="merge_gate_blocked", blocked_reason="prior_pipeline_pr_open"
        )
        assert occupies_lane_slot(gated) is True


class TestDependsOnPrValidator:
    @pytest.mark.parametrize("value", [7, 1])
    def test_accepts_positive_int(self, value: int) -> None:
        blocker = Blocker(
            stage="pre_flight", reason="dependency_unmerged", depends_on_pr=value
        )
        assert blocker.depends_on_pr == value

    @pytest.mark.parametrize("value", [True, False, 0, -3, "7", 7.0, [], {}])
    def test_rejects_non_positive_int(self, value: object) -> None:
        with pytest.raises(ValidationError):
            Blocker(
                stage="pre_flight", reason="dependency_unmerged", depends_on_pr=value
            )

    def test_defaults_to_none(self) -> None:
        blocker = Blocker(stage="pre_flight", reason="dependency_unmerged")
        assert blocker.depends_on_pr is None

    def test_helper_matches_model(self) -> None:
        assert parse_depends_on_pr(7) == 7
        assert parse_depends_on_pr(True) is None
        assert parse_depends_on_pr("7") is None


class TestRoutingStamp:
    def test_depends_on_pr_read_only_for_dependency_reason(self) -> None:
        assert _dependency_park_pr("dependency_unmerged", {"depends_on_pr": 88}) == 88

    def test_depends_on_pr_ignored_for_other_reasons(self) -> None:
        assert _dependency_park_pr("agent_block", {"depends_on_pr": 88}) is None

    def test_unparseable_depends_on_pr_yields_none(self) -> None:
        assert (
            _dependency_park_pr("dependency_unmerged", {"depends_on_pr": True}) is None
        )

    def test_stamp_sets_blocked_on_pr_from_dependency(self) -> None:
        task = make_task()
        _stamp_blocked_on_pr(task, "dependency_unmerged", {}, 88)
        assert task.blocked_on_pr == 88


def _watch(pr_number: int, state: str, client: str = "acme") -> WatchedPr:
    return WatchedPr(
        pr_url=f"https://github.com/foo/bar/pull/{pr_number}",
        repo="foo/bar",
        pr_number=pr_number,
        client=client,
        source="dependency_hold",
        pr_state=PrState(state=state),
    )


class TestServeTickRelease:
    def test_operator_hold_releases_on_merge_under_default_policy(self) -> None:
        seed_queue([_hold(session_id=None)])
        save_dev_queue(
            DevQueueStore(
                tasks=load_dev_queue().tasks,
                watched_prs=[_watch(42, "MERGED")],
            )
        )

        released = release_stale_gated_tasks()

        assert released == ["ACM-1"]
        assert queue_row().status == QueueItemStatus.PENDING
        [event] = events_of(OrchestratorEventType.SESSION_REAP_PROPOSED)
        assert event.payload["proposed_action"] == "release_stale_gate_variant_b"

    def test_worker_park_under_signal_only_only_stamps(self) -> None:
        seed_queue([_hold(session_id="sess-1")])
        save_dev_queue(
            DevQueueStore(
                tasks=load_dev_queue().tasks,
                watched_prs=[_watch(42, "MERGED")],
            )
        )

        release_stale_gated_tasks()

        row = queue_row()
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.session_id == "sess-1"
        assert row.stale_gate_detected_at is not None

    def test_closed_unmerged_hold_never_releases_and_latches_one_event(self) -> None:
        seed_queue([_hold(session_id=None)])
        save_dev_queue(
            DevQueueStore(
                tasks=load_dev_queue().tasks,
                watched_prs=[_watch(42, "CLOSED")],
            )
        )

        assert release_stale_gated_tasks() == []
        assert release_stale_gated_tasks() == []

        assert queue_row().status == QueueItemStatus.BLOCKED_ON_USER
        [event] = events_of(OrchestratorEventType.SESSION_REAP_PROPOSED)
        assert event.payload["proposed_action"] == "dependency_hold_pr_closed_unmerged"
        assert event.payload["evidence"] == {"blocked_on_pr": 42}

    def test_unknown_pr_state_fails_closed(self) -> None:
        seed_queue([_hold(session_id=None)])
        save_dev_queue(
            DevQueueStore(
                tasks=load_dev_queue().tasks,
                watched_prs=[
                    WatchedPr(
                        pr_url="https://github.com/foo/bar/pull/42",
                        repo="foo/bar",
                        pr_number=42,
                        client="acme",
                        source="dependency_hold",
                    )
                ],
            )
        )

        assert release_stale_gated_tasks() == []
        assert queue_row().stale_gate_detected_at is None
        assert events_of(OrchestratorEventType.SESSION_REAP_PROPOSED) == []
