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
from cw.auto_dev_result import AutoDevResult
from cw.codex_stage_completion import (
    clear_stage_blocked_marker,
    complete_session_as_unexpected_error,
    complete_session_through_door,
    revert_claimed_row_after_unexpected_error,
    save_stage_blocked_marker,
    stamp_session_id_on_running_task,
)
from cw.config import load_state
from cw.dev_queue import add_ticket, load_dev_queue
from cw.dispatch_state import load_executor_blocked_markers
from cw.local_runner import UNEXPECTED_ERROR
from cw.models import QueueItemStatus, SessionStatus, Stage, TicketTask
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
            sid="sid-d", payload={"status": "x"}, guard_already_completed=True
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
