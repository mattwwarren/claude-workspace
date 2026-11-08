"""CLI end-to-end: ``cw dev-queue approve`` as the review-less release (#2779).

On a lane with no review stage, ``approve`` of a finalize Step 4.0 park binds
an operator review approval to the branch head and prints that head and the
finalize requeue that ships it. The library records the only
``TICKET_APPROVED`` event; the CLI adds none and closes no orphan. Inside a
dispatch worker the command is refused before any of it runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.config import dev_queue_file
from cw.dev_queue.review_less_release import REVIEW_LESS_RELEASE_REASON
from cw.models import DEFAULT_LANE, OrchestratorEventType, QueueItemStatus, Stage
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    ROW_HEAD,
    STEP40_PARK,
    bound_session_for,
    events_of,
    invoke_dev_queue,
    make_blocked,
    patch_roster,
    patch_row_head,
    queue_row,
    seed_queue,
    stdout_lines,
    write_lane_client,
)

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

_RELEASE_ECHO = (
    f"Review-less release recorded for {ACME_TICKET} ({ACME}):"
    f" approver_kind=operator, branch head {ROW_HEAD[:12]}. This lane has no"
    " review stage, so nothing reviewed that head and you are vouching for it;"
    " inspect it first (run `git log --oneline` in the ticket's worktree). The"
    f" row is still parked; run `cw dev-queue requeue {ACME_TICKET} --client"
    f" {ACME} --stage finalize` to ship it. Finalize ships only while that"
    " commit is still in its history and nothing but merges of the default"
    " branch lands after it; any other later commit parks it again, to be"
    " re-approved with this command."
)


@pytest.fixture(autouse=True)
def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lane_client(
        ACME,
        tmp_path / "ws",
        lane_stages={DEFAULT_LANE: ("plan", "impl", "finalize")},
    )
    patch_roster(monkeypatch, set())
    _enter_operator_machine(monkeypatch, tmp_path)


def _seed() -> None:
    task = make_blocked(
        stage=Stage.FINALIZE,
        disposition="review_pending_approval",
        scope_hint="large",
    )
    seed_queue([task], [bound_session_for(task, last_result=STEP40_PARK)])


def _approve() -> Result:
    return invoke_dev_queue("approve", ACME_TICKET, "-c", ACME)


def _no_orphan_close(*_a: object, **_kw: object) -> None:
    msg = "the review-less release must not close routed-result orphans"
    raise AssertionError(msg)


class TestReviewLessReleaseCli:
    def test_echoes_the_head_and_the_next_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_row_head(monkeypatch)
        monkeypatch.setattr(
            "cw.cli.dev_queue.approve._close_orphans_after_approve", _no_orphan_close
        )
        _seed()
        result = _approve()
        assert result.exit_code == 0, result.output
        assert stdout_lines(result)[0] == _RELEASE_ECHO
        row = queue_row()
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.review_approved_sha == ROW_HEAD
        (event,) = events_of(OrchestratorEventType.TICKET_APPROVED)
        assert event.payload["reason"] == REVIEW_LESS_RELEASE_REASON
        assert event.payload["reviewed_sha"] == ROW_HEAD

    def test_refused_inside_a_dispatch_worker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []

        def _spy(*args: object, **_kw: object) -> str:
            calls.append(args)
            return ROW_HEAD

        monkeypatch.setattr("cw.dev_queue.review_less_release._row_head", _spy)
        _write_session_context(tmp_path, headless=True)
        _seed()
        before = dev_queue_file().read_bytes()
        result = _approve()
        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert calls == []
        assert dev_queue_file().read_bytes() == before
        assert events_of(OrchestratorEventType.TICKET_APPROVED) == []
