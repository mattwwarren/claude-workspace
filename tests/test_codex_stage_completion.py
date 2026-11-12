"""Stage-neutral completion scaffolding (#3054): marker lifecycle, door, revert.

The thread-level tests drive ``_run_codex_review_and_complete`` through the same
helpers ``tests/test_codex_background.py`` uses, so the marker ordering is pinned
on the real call path rather than on the helpers alone.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from cw import codex_background, codex_stage_completion
from cw._lock_guard import LockRank, is_rank_held
from cw.auto_dev_result import AutoDevResult
from cw.codex_review import (
    _CODEX_FINALIZE_BLOCKED_NEXT_ACTIONS,
    _CODEX_PLAN_BLOCKED_NEXT_ACTIONS,
    make_codex_blocked,
)
from cw.codex_stage_completion import (
    STAGE_FAULTS,
    clear_stage_blocked_marker,
    complete_session_as_unexpected_error,
    complete_session_through_door,
    flush_late_usage,
    revert_claimed_row_after_unexpected_error,
    run_guarded_stage,
    save_stage_blocked_marker,
    stamp_session_id_on_running_task,
)
from cw.codex_usage import UsageAccumulator
from cw.config import load_state, save_state
from cw.dev_queue import add_ticket, load_dev_queue
from cw.dispatch_state import load_executor_blocked_markers
from cw.exceptions import CwError
from cw.local_runner import UNEXPECTED_ERROR
from cw.models import CwState, QueueItemStatus, SessionStatus, Stage, TicketTask
from cw.models.orchestrator_config.stage import ModelPrice
from cw.models.usage import (
    TurnUsage,
    UsageSnapshot,
    UsageStage,
    UsageState,
    UsageUnavailable,
    UsageUnavailableReason,
)
from tests._shared.session_seeding import _make_daemon_session
from tests.test_codex_background import _client, _run, _seed_session


def _running_task(sid: str) -> TicketTask:
    add_ticket(
        TicketTask(
            ticket_id="T-stage",
            client="test",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id=sid,
        )
    )
    return TicketTask(ticket_id="T-stage", client="test", stage=Stage.REVIEW)


class TestMarkerHelpers:
    def test_save_writes_marker_and_clear_removes_it_idempotently(
        self, tmp_config_dir: Path
    ) -> None:
        save_stage_blocked_marker(
            client_name="test", ticket_id="T-stage", session_id="sid-m"
        )
        markers = load_executor_blocked_markers()
        assert len(markers) == 1
        (marker,) = markers.values()
        assert marker.ticket_id == "T-stage"
        assert marker.executor == "codex"
        assert marker.session_id == "sid-m"
        assert marker.reviewer_role is None

        clear_stage_blocked_marker("test", "T-stage")
        assert load_executor_blocked_markers() == {}
        # Second clear on an absent marker must not raise.
        clear_stage_blocked_marker("test", "T-stage")


class TestThreadMarkerSemantics:
    def test_save_failure_completes_as_unexpected_error_and_reverts_row(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        """A failing marker save is caught by the sanctioned except, not raised.

        The save sits inside the try, so the session ends COMPLETED/blocked and the
        claimed row goes back to PENDING. Nothing escapes the thread function, and
        the review itself never runs.
        """
        worktree = make_git_repo("wt-stage-save")
        _seed_session("sid-save")
        task = _running_task("sid-save")
        with (
            patch(
                "cw.codex_stage_completion.save_executor_blocked_marker",
                side_effect=OSError("disk gone"),
            ),
            patch("cw.codex_background._review_and_confirm") as review,
        ):
            _run(sid="sid-save", task=task, worktree=worktree, client=_client(worktree))

        review.assert_not_called()
        session = load_state().sessions[0]
        assert session.status is SessionStatus.COMPLETED
        persisted = AutoDevResult.model_validate(session.last_result)
        assert persisted.blocker is not None
        assert persisted.blocker.reason == UNEXPECTED_ERROR
        stored = load_dev_queue().tasks[0]
        assert stored.status is QueueItemStatus.PENDING
        assert stored.spawn_error_count == 1

    def test_handlers_run_before_marker_clear_on_exception(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-stage-order")
        _seed_session("sid-order")
        task = _running_task("sid-order")
        order: list[str] = []
        with (
            patch(
                "cw.codex_background._review_and_confirm",
                side_effect=RuntimeError("review boom"),
            ),
            patch(
                "cw.codex_background._complete_session_as_unexpected_error",
                side_effect=lambda *_a, **_k: order.append("complete"),
            ),
            patch(
                "cw.dispatch.claim._revert_claimed_task_to_pending",
                side_effect=lambda *_a, **_k: order.append("revert"),
            ),
            patch(
                "cw.codex_background.clear_stage_blocked_marker",
                side_effect=lambda *_a, **_k: order.append("clear"),
            ),
        ):
            _run(
                sid="sid-order", task=task, worktree=worktree, client=_client(worktree)
            )

        assert order == ["complete", "revert", "clear"]

    def test_marker_cleared_on_base_exception(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        """A BaseException the except clause does not catch still clears the marker."""
        worktree = make_git_repo("wt-stage-base")
        _seed_session("sid-base")
        task = _running_task("sid-base")
        with (
            patch(
                "cw.codex_background._review_and_confirm",
                side_effect=KeyboardInterrupt,
            ),
            patch("cw.codex_background.clear_stage_blocked_marker") as clear,
            pytest.raises(KeyboardInterrupt),
        ):
            _run(sid="sid-base", task=task, worktree=worktree, client=_client(worktree))
        clear.assert_called_once_with("test", "T-stage")


class TestDoorAndUnexpectedError:
    def test_through_door_passes_guard_flag_under_lock(
        self, tmp_config_dir: Path
    ) -> None:
        with patch("cw.executor._complete_session_via_door") as door:
            complete_session_through_door(
                "sid-d", {"status": "x"}, guard_already_completed=True
            )
        door.assert_called_once_with(
            sid="sid-d",
            payload={"status": "x"},
            guard_already_completed=True,
            usage=None,
        )

    def test_unexpected_error_completes_real_session_without_reentering_lock(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Real sessions_lock, no patches: a re-entrant lock would raise here."""
        worktree = make_git_repo("wt-stage-lock")
        _seed_session("sid-lock")
        task = TicketTask(ticket_id="T-lock", client="test", stage=Stage.REVIEW)

        complete_session_as_unexpected_error("sid-lock", task, worktree)

        session = load_state().sessions[0]
        assert session.status is SessionStatus.COMPLETED
        persisted = AutoDevResult.model_validate(session.last_result)
        assert persisted.blocker is not None
        assert persisted.blocker.reason == UNEXPECTED_ERROR

    def test_unexpected_error_carries_the_given_stage(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-stage-plan")
        _seed_session("sid-plan")
        task = TicketTask(ticket_id="T-plan", client="test", stage=Stage.PLAN)

        complete_session_as_unexpected_error(
            "sid-plan", task, worktree, stage_reached="stage1_plan"
        )

        persisted = AutoDevResult.model_validate(load_state().sessions[0].last_result)
        assert persisted.stage_reached == "stage1_plan"
        assert persisted.next_actions == _CODEX_PLAN_BLOCKED_NEXT_ACTIONS

    def test_unexpected_error_at_the_merge_gate_carries_finalize_next_actions(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        """#3062: the finalize wire stage keys the finalize label, not review's."""
        worktree = make_git_repo("wt-stage-finalize")
        _seed_session("sid-fin")
        task = TicketTask(ticket_id="T-fin", client="test", stage=Stage.FINALIZE)

        complete_session_as_unexpected_error(
            "sid-fin", task, worktree, stage_reached="stage4a_merge_gate"
        )

        persisted = AutoDevResult.model_validate(load_state().sessions[0].last_result)
        assert persisted.stage_reached == "stage4a_merge_gate"
        assert persisted.blocker is not None
        assert persisted.blocker.reason == UNEXPECTED_ERROR
        assert persisted.next_actions == _CODEX_FINALIZE_BLOCKED_NEXT_ACTIONS


def _payload(make_git_repo: Callable[[str], Path]) -> dict[str, object]:
    """A schema-valid terminal payload for the door."""
    task = TicketTask(ticket_id="T-u", client="test", stage=Stage.REVIEW)
    return make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=make_git_repo("wt-usage-payload"),
        reason=UNEXPECTED_ERROR,
        stage_reached="stage3_review",
    ).model_dump(mode="json")


def _acc(session_id: str = "sid-u") -> UsageAccumulator:
    price = ModelPrice(
        input_usd_per_mtok=1.0, cached_input_usd_per_mtok=0.5, output_usd_per_mtok=2.0
    )
    return UsageAccumulator(session_id, "T-u", {"gpt-x": price})


def _record(acc: UsageAccumulator, kind: UsageStage = UsageStage.PLAN) -> None:
    acc.record_turn(
        kind, kind.value, "gpt-x", TurnUsage(1000, 0, 0, 100, 0), completed=True
    )


class TestUsagePersistence:
    """#3063: the door persists the run's usage; parse and price run unlocked."""

    def test_door_persists_usage_and_cost_breakdown(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        _seed_session("sid-u")
        acc = _acc()
        _record(acc)

        complete_session_through_door("sid-u", _payload(make_git_repo), usage=acc)

        session = load_state().sessions[0]
        assert session.usage is not None
        assert session.usage.state is UsageState.RECORDED
        assert session.usage.by_stage[UsageStage.PLAN].input_tokens == 1000
        assert session.cost_breakdown == pytest.approx({"gpt-x": 0.0012})

    def test_default_no_usage_leaves_both_fields_none(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        _seed_session("sid-u")

        complete_session_through_door("sid-u", _payload(make_git_repo))

        session = load_state().sessions[0]
        assert session.usage is None
        assert session.cost_breakdown is None

    def test_snapshot_and_parse_run_without_the_sessions_lock(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        _seed_session("sid-u")
        acc = _acc()
        _record(acc)
        held: dict[str, bool] = {}
        original = UsageAccumulator.snapshot

        def spy_snapshot(self: UsageAccumulator) -> UsageSnapshot | None:
            held["snapshot"] = is_rank_held(LockRank.SESSIONS)
            return original(self)

        def spy_door(**kwargs: object) -> None:
            held["door"] = is_rank_held(LockRank.SESSIONS)
            held["ready_snapshot"] = isinstance(kwargs["usage"], UsageSnapshot)

        monkeypatch.setattr(UsageAccumulator, "snapshot", spy_snapshot)
        monkeypatch.setattr("cw.executor._complete_session_via_door", spy_door)

        complete_session_through_door("sid-u", _payload(make_git_repo), usage=acc)

        assert held == {"snapshot": False, "door": True, "ready_snapshot": True}

    def test_door_retry_writes_the_same_snapshot_idempotently(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        _seed_session("sid-u")
        acc = _acc()
        _record(acc)

        complete_session_through_door("sid-u", _payload(make_git_repo), usage=acc)
        first = load_state().sessions[0].usage
        complete_session_through_door("sid-u", _payload(make_git_repo), usage=acc)

        assert load_state().sessions[0].usage == first

    def test_unexpected_error_persists_tokens_spent_before_a_crash(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-usage-crash")
        _seed_session("sid-u")
        acc = _acc()
        _record(acc)
        task = TicketTask(ticket_id="T-u", client="test", stage=Stage.REVIEW)

        complete_session_as_unexpected_error("sid-u", task, worktree, usage=acc)

        usage = load_state().sessions[0].usage
        assert usage is not None
        assert usage.total_tokens == 1100

    def test_guarded_stage_carries_usage_into_the_crash_path(
        self, tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-usage-guarded")
        _seed_session("sid-u")
        acc = _acc()
        task = _running_task("sid-u")

        def body() -> None:
            _record(acc)
            msg = "boom"
            raise CwError(msg)

        run_guarded_stage(
            client_name="test",
            task=task,
            worktree=worktree,
            sid="sid-u",
            stage_reached="stage1_plan",
            body=body,
            usage=acc,
        )

        usage = load_state().sessions[0].usage
        assert usage is not None
        assert usage.total_tokens == 1100


class TestFlushLateUsage:
    def test_rewrites_the_superset_once_then_is_a_no_op(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        _seed_session("sid-u")
        acc = _acc()
        _record(acc)
        complete_session_through_door("sid-u", _payload(make_git_repo), usage=acc)
        _record(acc, UsageStage.TRACKER_IO)
        held: list[bool] = []
        original = UsageAccumulator.snapshot

        def spy_snapshot(self: UsageAccumulator) -> UsageSnapshot | None:
            held.append(is_rank_held(LockRank.SESSIONS))
            return original(self)

        monkeypatch.setattr(UsageAccumulator, "snapshot", spy_snapshot)

        flush_late_usage("sid-u", acc)
        usage = load_state().sessions[0].usage
        assert usage is not None
        assert set(usage.by_stage) == {UsageStage.PLAN, UsageStage.TRACKER_IO}
        assert usage.total_tokens == 2200

        flush_late_usage("sid-u", acc)
        assert held == [False]

    def test_no_op_when_the_session_id_is_absent_from_state(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        save_state(CwState(sessions=[_make_daemon_session(id="other")]))
        acc = _acc("gone")
        _record(acc)
        before = load_state().model_dump(mode="json")

        flush_late_usage("gone", acc)

        assert load_state().model_dump(mode="json") == before
        assert load_state().sessions[0].usage is None

    def test_clean_accumulator_never_touches_state(self, tmp_config_dir: Path) -> None:
        with patch("cw.codex_stage_completion.mutate_state") as mutate:
            flush_late_usage("sid-u", _acc())

        mutate.assert_not_called()

    def test_writes_through_mutate_state_once(self, tmp_config_dir: Path) -> None:
        _seed_session("sid-u")
        acc = _acc()
        _record(acc)

        with patch(
            "cw.codex_stage_completion.mutate_state",
            wraps=codex_stage_completion.mutate_state,
        ) as mutate:
            flush_late_usage("sid-u", acc)

        mutate.assert_called_once()
        assert load_state().sessions[0].usage is not None

    def test_dirty_accumulator_with_nothing_recorded_skips_the_write(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        # A recorded-then-ignored turn marks the accumulator dirty but leaves
        # nothing to persist.
        acc = _acc()
        acc.record_turn(
            UsageStage.PLAN,
            "plan",
            "gpt-x",
            UsageUnavailable(UsageUnavailableReason.TURN_FAILED),
            completed=True,
        )

        with patch("cw.codex_stage_completion.mutate_state") as mutate:
            flush_late_usage("sid-u", acc)

        mutate.assert_not_called()


class TestRunGuardedStage:
    def _guard(self, worktree: Path, body: Callable[[], None]) -> None:
        run_guarded_stage(
            client_name="test",
            task=_running_task("sid-g"),
            worktree=worktree,
            sid="sid-g",
            stage_reached="stage1_plan",
            body=body,
        )

    def test_runs_the_body_with_the_marker_held(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        seen: list[int] = []
        self._guard(tmp_path, lambda: seen.append(len(load_executor_blocked_markers())))
        assert seen == [1]
        assert not load_executor_blocked_markers()

    @pytest.mark.parametrize("fault", [OSError("io"), CwError("cw"), ValueError("v")])
    def test_a_stage_fault_completes_and_reverts(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
        fault: Exception,
    ) -> None:
        assert isinstance(fault, STAGE_FAULTS)
        worktree = make_git_repo("wt-guard")
        _seed_session("sid-g")

        def _boom() -> None:
            raise fault

        self._guard(worktree, _boom)

        persisted = AutoDevResult.model_validate(load_state().sessions[0].last_result)
        assert persisted.blocker is not None
        assert persisted.blocker.reason == UNEXPECTED_ERROR
        assert persisted.stage_reached == "stage1_plan"
        assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING
        assert not load_executor_blocked_markers()

    def test_other_exceptions_propagate_and_still_clear_the_marker(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        def _interrupt() -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            self._guard(tmp_path, _interrupt)
        assert not load_executor_blocked_markers()
        assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


class TestRowHelpers:
    def test_revert_returns_only_the_matching_session_row(
        self, tmp_config_dir: Path
    ) -> None:
        add_ticket(
            TicketTask(
                ticket_id="T-rev",
                client="test",
                stage=Stage.REVIEW,
                status=QueueItemStatus.RUNNING,
                session_id="sid-owner",
            )
        )
        revert_claimed_row_after_unexpected_error(
            client_name="test", ticket_id="T-rev", session_id="sid-other"
        )
        assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING

        revert_claimed_row_after_unexpected_error(
            client_name="test", ticket_id="T-rev", session_id="sid-owner"
        )
        stored = load_dev_queue().tasks[0]
        assert stored.status is QueueItemStatus.PENDING
        assert stored.spawn_error_count == 1

    def test_stamp_sets_session_id_on_matching_running_row(
        self, tmp_config_dir: Path
    ) -> None:
        add_ticket(
            TicketTask(
                ticket_id="T-stamp",
                client="test",
                stage=Stage.REVIEW,
                status=QueueItemStatus.RUNNING,
            )
        )
        created_at = load_dev_queue().tasks[0].created_at
        stamp_session_id_on_running_task(
            client_name="test",
            ticket_id="T-stamp",
            session_id="sid-stamp",
            created_at=created_at,
        )
        assert load_dev_queue().tasks[0].session_id == "sid-stamp"


class TestReExports:
    def test_background_module_still_exposes_moved_names(self) -> None:
        assert (
            codex_background._stamp_session_id_on_running_task
            is codex_stage_completion.stamp_session_id_on_running_task
        )
        assert (
            codex_background._complete_session_as_unexpected_error
            is codex_stage_completion.complete_session_as_unexpected_error
        )
        assert "_stamp_session_id_on_running_task" in codex_background.__all__

    def test_background_module_stays_under_size_pin(self) -> None:
        source = Path(codex_background.__file__).read_text(encoding="utf-8")
        assert len(source.splitlines()) < 800

    def test_cold_import_has_no_cycle(self) -> None:
        subprocess.run(
            [sys.executable, "-c", "import cw.codex_stage_completion"],
            check=True,
            capture_output=True,
        )
