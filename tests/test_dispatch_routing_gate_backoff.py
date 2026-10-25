"""Rule 5 gate-block backoff through the real routing path (GitHub #2717).

Every park here goes through ``apply_staged_decision`` via the shared
``park_via_routing`` builder, so ``gate_block``/``next_eligible_at`` are
stamped exactly as production stamps them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from freezegun import freeze_time

from cw.dev_queue import load_dev_queue, save_dev_queue, transition_task_status
from cw.dev_queue.gate_backoff import clear_gate_block_wait
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.queue_rows import _apply_spawn_success_fields, _revert_claimed_task_to_pending
from cw.reconcile.usage_limit_mid_turn import _requeue
from tests._clients_yaml import write_clients_yaml
from tests._reconcile_helpers import gate_blocked_last_result, park_via_routing

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedEvent

_CLIENT = "test-client"
_FROZEN = "2026-07-16 12:00:00"
_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_PRODUCER = "cw.dispatch.routing.stage_failure"
_NOTE_1 = "occurrence=1 next_eligible_at=2026-07-16T12:01:00+00:00"
_AGENT_BLOCK_RESULT: dict[str, object] = {
    "status": "blocked",
    "blocker": {"stage": "stage2_impl", "reason": "agent_block"},
}


def _gate_park(
    tmp_path: Path,
    *,
    ticket_id: str = "GB-1",
    pr_number: int | None = 88,
    task: TicketTask | None = None,
    frozen: str = _FROZEN,
) -> TicketTask:
    with freeze_time(frozen):
        return park_via_routing(
            ticket_id,
            _CLIENT,
            tmp_path,
            "merge_gate_blocked",
            gate_blocked_last_result(pr_number),
            task=task,
        )


def _pages(
    capture_events: Callable[..., list[CapturedEvent]],
) -> list[CapturedEvent]:
    return capture_events(_PRODUCER, OrchestratorEventType.SESSION_NEEDS_ATTENTION)


def _breadcrumbs(pages: list[CapturedEvent]) -> list[object]:
    return [payload["breadcrumbs"] for _, payload, _ in pages]


class TestGateBlockPark:
    def test_client_rollout_can_disable_backoff(self, tmp_path: Path) -> None:
        client = ClientConfig(
            name=_CLIENT,
            workspace_path=tmp_path,
            gate_block_backoff_enabled=False,
        )

        task = park_via_routing(
            "GB-DISABLED",
            _CLIENT,
            tmp_path,
            "merge_gate_blocked",
            gate_blocked_last_result(),
            client_config=client,
        )

        assert task.gate_block is None
        assert task.next_eligible_at is None

    def test_disabled_rollout_releases_already_stamped_gate_wait(
        self, tmp_path: Path
    ) -> None:
        task = _gate_park(tmp_path, ticket_id="GB-STAMPED")
        client = ClientConfig(
            name=_CLIENT,
            workspace_path=tmp_path,
            gate_block_backoff_enabled=False,
        )

        park_via_routing(
            "GB-STAMPED",
            _CLIENT,
            tmp_path,
            "merge_gate_blocked",
            gate_blocked_last_result(),
            task=task,
            client_config=client,
        )

        assert task.gate_block is not None
        assert task.gate_block.count == 1
        assert task.next_eligible_at is None

    def test_fingerprint_stage_is_the_post_walk_stage(self, tmp_path: Path) -> None:
        """A later-stage sentinel walks the row; the run keys on the landing stage."""
        task = _gate_park(tmp_path, ticket_id="GB-WALK")
        assert task.gate_block is not None
        assert task.gate_block.fingerprint.split("|")[3] == Stage.IMPL.value

        # Re-arm the row one rung behind; the sentinel reports IMPL, so the
        # walk lands on IMPL and the run must continue, not restart.
        transition_task_status(task, QueueItemStatus.RUNNING)
        task.stage = Stage.PLAN
        last_result = {**gate_blocked_last_result(), "stage_reached": "stage2_impl"}
        with freeze_time(_FROZEN):
            park_via_routing(
                "GB-WALK",
                _CLIENT,
                tmp_path,
                "merge_gate_blocked",
                last_result,
                task=task,
            )

        assert task.stage == Stage.IMPL
        assert task.gate_block is not None
        assert task.gate_block.count == 2

    def test_first_park_stamps_backoff_and_page_note(
        self,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        pages = _pages(capture_events)

        task = _gate_park(tmp_path)

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.gate_block is not None
        assert task.gate_block.count == 1
        assert task.next_eligible_at == _NOW + timedelta(seconds=60)
        assert _breadcrumbs(pages) == [f"prior_pipeline_pr_open {_NOTE_1}"]

    def test_attempt_charge_matches_a_non_gate_park(self, tmp_path: Path) -> None:
        """Rule 5's unproductive charge is untouched (#2695 owns it): a gate
        park is charged exactly like a non-gate Rule 5 park on the same
        (commit-less) evidence."""
        gate = _gate_park(tmp_path)
        non_gate = park_via_routing(
            "GB-CHARGE",
            _CLIENT,
            tmp_path,
            "blocked",
            {
                "status": "blocked",
                "blocker": {"stage": "stage4a", "reason": "automerge_not_armed"},
            },
        )

        assert gate.unproductive_attempts == non_gate.unproductive_attempts

    def test_second_park_same_pr_escalates(
        self,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        task = _gate_park(tmp_path)
        pages = _pages(capture_events)

        _gate_park(tmp_path, task=task)

        assert task.gate_block is not None
        assert task.gate_block.count == 2
        assert task.next_eligible_at == _NOW + timedelta(seconds=120)
        assert _breadcrumbs(pages) == [
            "prior_pipeline_pr_open occurrence=2"
            " next_eligible_at=2026-07-16T12:02:00+00:00"
        ]

    def test_different_blocking_pr_resets(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)
        _gate_park(tmp_path, task=task)

        _gate_park(tmp_path, task=task, pr_number=99)

        assert task.gate_block is not None
        assert task.gate_block.count == 1

    def test_moved_stage_base_ref_resets(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)
        _gate_park(tmp_path, task=task)
        task.stage_base_ref = "moved-head"

        _gate_park(tmp_path, task=task)

        assert task.gate_block is not None
        assert task.gate_block.count == 1

    def test_no_blocker_note_stands_alone(
        self,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        pages = _pages(capture_events)

        with freeze_time(_FROZEN):
            park_via_routing(
                "GB-NB",
                _CLIENT,
                tmp_path,
                "merge_gate_blocked",
                gate_blocked_last_result(reason=None),
            )

        assert _breadcrumbs(pages) == [_NOTE_1]

    def test_blocked_with_prior_pipeline_reason_is_gate_block(
        self,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        pages = _pages(capture_events)

        with freeze_time(_FROZEN):
            task = park_via_routing(
                "GB-BL",
                _CLIENT,
                tmp_path,
                "blocked",
                {**gate_blocked_last_result(), "status": "blocked"},
            )

        assert task.gate_block is not None
        assert _breadcrumbs(pages) == [f"prior_pipeline_pr_open {_NOTE_1}"]

    def test_stale_dispatch_is_excluded(
        self,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        pages = _pages(capture_events)

        task = park_via_routing(
            "GB-SD",
            _CLIENT,
            tmp_path,
            "stale_dispatch",
            {
                "status": "stale_dispatch",
                "blocker": {
                    "stage": "stage1_pre_flight",
                    "reason": "pr_already_open",
                    "details": "PR #1899 (dev/1862) is open and awaiting review.",
                },
                "commits": [],
            },
        )

        assert task.gate_block is None
        assert _breadcrumbs(pages) == ["pr_already_open"]

    def test_automerge_not_armed_is_excluded(
        self,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        pages = _pages(capture_events)

        task = park_via_routing(
            "GB-AM",
            _CLIENT,
            tmp_path,
            "blocked",
            {
                "status": "blocked",
                "blocker": {"stage": "stage4a", "reason": "automerge_not_armed"},
            },
        )

        assert task.gate_block is None
        assert task.next_eligible_at is None
        assert _breadcrumbs(pages) == ["automerge_not_armed"]


class TestNonGateOutcomeReset:
    def test_blocked_outcome_resets_record_keeps_wait(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)
        wait = task.next_eligible_at

        park_via_routing(
            "GB-1", _CLIENT, tmp_path, "blocked", _AGENT_BLOCK_RESULT, task=task
        )

        assert task.gate_block is None
        assert task.next_eligible_at == wait

    def test_no_op_resets_record(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)

        park_via_routing(
            "GB-1", _CLIENT, tmp_path, "no_op", {"status": "no_op"}, task=task
        )

        assert task.status == QueueItemStatus.COMPLETED
        assert task.gate_block is None

    def test_stage_complete_advance_resets_record(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)

        park_via_routing(
            "GB-1",
            _CLIENT,
            tmp_path,
            "stage_complete",
            {"status": "stage_complete"},
            task=task,
        )

        assert task.stage == Stage.REVIEW
        assert task.gate_block is None


class TestSharedClockAndCounters:
    def test_gate_park_leaves_spawn_error_count(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)

        assert task.spawn_error_count == 0

    def test_transition_keeps_gate_block_and_wait(self, tmp_path: Path) -> None:
        """ADR-0014 pin: ``transition_task_status`` clears neither field."""
        task = _gate_park(tmp_path)
        record, wait = task.gate_block, task.next_eligible_at

        transition_task_status(task, QueueItemStatus.PENDING)

        assert task.gate_block == record
        assert task.next_eligible_at == wait

    def test_spawn_success_clears_wait_keeps_record(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)
        task.spawn_error_count = 2

        _apply_spawn_success_fields(task, session_id="sess-new")

        assert task.next_eligible_at is None
        assert task.spawn_error_count == 0
        assert task.gate_block is not None
        assert task.gate_block.count == 1

    @staticmethod
    def _rearm_running(task: TicketTask) -> None:
        transition_task_status(task, QueueItemStatus.RUNNING)
        save_dev_queue(DevQueueStore(tasks=[task]))

    def test_spawn_failure_revert_counts_separately(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)
        self._rearm_running(task)

        assert _revert_claimed_task_to_pending(
            _CLIENT, "GB-1", stamp_backoff=True, created_at=task.created_at
        )

        stored = load_dev_queue().tasks[0]
        assert stored.spawn_error_count == 1
        assert stored.gate_block is not None
        assert stored.gate_block.count == 1

    def test_usage_limit_defer_window_survives_clear(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)
        self._rearm_running(task)

        with freeze_time(_FROZEN):
            assert _revert_claimed_task_to_pending(
                _CLIENT,
                "GB-1",
                defer_for=timedelta(hours=1),
                created_at=task.created_at,
            )
        stored = load_dev_queue().tasks[0]
        clear_gate_block_wait(stored)

        assert stored.next_eligible_at == _NOW + timedelta(hours=1)

    def test_mid_turn_requeue_window_survives_clear(self, tmp_path: Path) -> None:
        task = _gate_park(tmp_path)
        until = _NOW + timedelta(hours=2)

        _requeue(task, until=until)
        clear_gate_block_wait(task)

        assert task.next_eligible_at == until


class TestClaimScreen:
    def _park_pending(self, client: ClientConfig) -> None:
        task = _gate_park(client.workspace_path)
        transition_task_status(task, QueueItemStatus.PENDING)
        save_dev_queue(DevQueueStore(tasks=[task]))

    def test_claim_held_inside_window_without_mutation(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """ADR-0014 invariant 1: the screen only delays, it writes nothing."""
        from cw.dispatch.claim import _claim_next_pending

        write_clients_yaml(sample_client_config)
        self._park_pending(sample_client_config)
        before = load_dev_queue().tasks[0].model_dump()

        with freeze_time("2026-07-16 12:00:30"):
            claimed = _claim_next_pending(
                _CLIENT,
                lane="default",
                client=sample_client_config,
                config=simple_config,
            )

        assert claimed == (None, True)
        assert load_dev_queue().tasks[0].model_dump() == before

    def test_disabling_backoff_releases_a_waiting_row(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Flipping the switch off frees an already-stamped wait."""
        from cw.dispatch.claim import _claim_next_pending

        write_clients_yaml(sample_client_config)
        self._park_pending(sample_client_config)
        disabled = sample_client_config.model_copy(
            update={"gate_block_backoff_enabled": False}
        )

        with freeze_time("2026-07-16 12:00:30"):
            claimed, _ = _claim_next_pending(
                _CLIENT,
                lane="default",
                client=disabled,
                config=simple_config,
            )

        assert claimed is not None
        assert claimed.ticket_id == "GB-1"

    def test_disabled_wait_is_cleared_before_non_gate_reclaim(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        tmp_path: Path,
    ) -> None:
        """A disabled gate wait cannot block a re-claim after a non-gate route."""
        from cw.dispatch.claim import _claim_next_pending

        write_clients_yaml(sample_client_config)
        self._park_pending(sample_client_config)
        disabled = sample_client_config.model_copy(
            update={"gate_block_backoff_enabled": False}
        )

        with freeze_time("2026-07-16 12:00:30"):
            claimed, _ = _claim_next_pending(
                _CLIENT,
                lane="default",
                client=disabled,
                config=simple_config,
            )

        assert claimed is not None
        assert claimed.next_eligible_at is None

        parked = park_via_routing(
            "GB-1",
            _CLIENT,
            tmp_path,
            "blocked",
            _AGENT_BLOCK_RESULT,
            task=claimed,
            client_config=disabled,
        )
        transition_task_status(parked, QueueItemStatus.PENDING)
        save_dev_queue(DevQueueStore(tasks=[parked]))

        with freeze_time("2026-07-16 12:00:30"):
            reclaimed, _ = _claim_next_pending(
                _CLIENT,
                lane="default",
                client=disabled,
                config=simple_config,
            )

        assert reclaimed is not None
        assert reclaimed.ticket_id == "GB-1"

    def test_disabling_backoff_keeps_a_usage_limit_window(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        from cw.dispatch.claim import _claim_next_pending

        write_clients_yaml(sample_client_config)
        self._park_pending(sample_client_config)
        store = load_dev_queue()
        store.tasks[0].next_eligible_at = _NOW + timedelta(hours=3)
        save_dev_queue(store)
        disabled = sample_client_config.model_copy(
            update={"gate_block_backoff_enabled": False}
        )

        with freeze_time("2026-07-16 12:00:30"):
            claimed = _claim_next_pending(
                _CLIENT,
                lane="default",
                client=disabled,
                config=simple_config,
            )

        assert claimed == (None, True)

    def test_claimed_after_window(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        from cw.dispatch.claim import _claim_next_pending

        write_clients_yaml(sample_client_config)
        self._park_pending(sample_client_config)

        with freeze_time("2026-07-16 12:01:01"):
            claimed, _ = _claim_next_pending(
                _CLIENT,
                lane="default",
                client=sample_client_config,
                config=simple_config,
            )

        assert claimed is not None
        assert claimed.ticket_id == "GB-1"
