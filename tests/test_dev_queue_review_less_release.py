"""``cw.dev_queue.review_less_release``: approve on a lane with no review stage (#2779).

On a lane whose pipeline has no review stage, a finalize Step 4.0 approval
park is released by the operator's plain ``approve``: it stamps an operator
review approval bound to the worktree HEAD at approve time, records one
``TICKET_APPROVED`` event first, and leaves the row parked for the finalize
requeue. Every other terminal row keeps the terminal refusal.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.config import dev_queue_file
from cw.context_queue_metadata import build_queue_metadata
from cw.dev_queue import requeue_ticket
from cw.dev_queue.approval import (
    REVIEW_APPROVED_SHA_KEY,
    REVIEW_APPROVER_KIND_KEY,
    REVIEW_LESS_RELEASE_KEY,
    _approve_ticket_locked,
    _raise_if_terminal_stage,
    approve_ticket,
)
from cw.dev_queue.review_less_release import REVIEW_LESS_RELEASE_REASON
from cw.dev_queue.storage import _lock
from cw.exceptions import ApproveGateError, GitTimeoutError
from cw.models import DEFAULT_LANE, OrchestratorEventType, QueueItemStatus, Stage
from cw.native_daemon import FakeNativeDaemonClient
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    REVIEW_PENDING,
    ROW_HEAD,
    STEP40_PARK,
    bound_session_for,
    cmd,
    events_of,
    make_blocked,
    patch_roster,
    patch_row_head,
    queue_row,
    seed_queue,
    write_lane_client,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_LOGIN = "octo-operator"
_NO_REVIEW = ("plan", "impl", "finalize")
_STALE_TIP: dict[str, object] = {
    **STEP40_PARK,
    "scope": {"tier": "small"},
    "friction_highlights": ["review_tip_check: stale (no_clean_round)"],
}
_NOW = "2026-11-08T12:00:00+00:00"
_EMPTY_SNAPSHOT = {"approved_at": None, "approver_kind": None, "reviewed_sha": None}


@pytest.fixture(autouse=True)
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lane_client(ACME, tmp_path / "ws", lane_stages={DEFAULT_LANE: _NO_REVIEW})
    patch_roster(monkeypatch, set())
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: _LOGIN)


def _seed(
    last_result: dict[str, object] | None = STEP40_PARK, **overrides: object
) -> TicketTask:
    kwargs: dict[str, object] = {
        "stage": Stage.FINALIZE,
        "disposition": "review_pending_approval",
        "scope_hint": "large",
    }
    kwargs.update(overrides)
    task = make_blocked(**kwargs)
    seed_queue([task], [bound_session_for(task, last_result=last_result)])
    return task


def _approved_events() -> list[dict[str, object]]:
    return [e.payload for e in events_of(OrchestratorEventType.TICKET_APPROVED)]


def _queue_bytes() -> bytes:
    return dev_queue_file().read_bytes()


def _assert_refused_unchanged(before: bytes, match: str) -> None:
    with pytest.raises(ApproveGateError, match=match):
        approve_ticket(ACME_TICKET, ACME)
    assert _queue_bytes() == before
    assert _approved_events() == []


class TestRelease:
    @freeze_time(_NOW)
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_row_head(monkeypatch)
        _seed()
        result = approve_ticket(ACME_TICKET, ACME)
        assert result[REVIEW_LESS_RELEASE_KEY] is True
        assert result[REVIEW_APPROVER_KIND_KEY] == "operator"
        assert result[REVIEW_APPROVED_SHA_KEY] == ROW_HEAD
        assert (result["from_stage"], result["to_stage"]) == ("finalize", "finalize")
        row = queue_row()
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.stage == Stage.FINALIZE
        assert row.review_approver_kind == "operator"
        assert row.review_approved_sha == ROW_HEAD
        assert row.review_approved_at is not None
        assert row.review_approved_at.isoformat() == _NOW
        assert _approved_events() == [
            {
                "ticket_id": ACME_TICKET,
                "client": ACME,
                "from_stage": "finalize",
                "to_stage": "finalize",
                "actor": _LOGIN,
                "reason": REVIEW_LESS_RELEASE_REASON,
                "reviewed_sha": ROW_HEAD,
                "review_approver_kind": "operator",
                "review_approval_before": _EMPTY_SNAPSHOT,
                "review_approval_after": {
                    "approved_at": _NOW,
                    "approver_kind": "operator",
                    "reviewed_sha": ROW_HEAD,
                },
            }
        ]
        (event,) = events_of(OrchestratorEventType.TICKET_APPROVED)
        assert event.correlation_id == ACME_TICKET

    def test_reapprove_overwrites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_row_head(monkeypatch)
        _seed()
        approve_ticket(ACME_TICKET, ACME)
        first = queue_row()
        patch_row_head(monkeypatch, "c" * 40)
        approve_ticket(ACME_TICKET, ACME)
        assert queue_row().review_approved_sha == "c" * 40
        second = _approved_events()[1]
        before = second["review_approval_before"]
        assert isinstance(before, dict)
        assert before["reviewed_sha"] == first.review_approved_sha

    def test_finalize_requeue_keeps_the_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_row_head(monkeypatch)
        _seed()
        approve_ticket(ACME_TICKET, ACME)
        requeue_ticket(
            ACME_TICKET, ACME, "finalize", native_daemon=FakeNativeDaemonClient()
        )
        row = queue_row()
        assert row.status == QueueItemStatus.PENDING
        assert row.review_approved_sha == ROW_HEAD
        approval = build_queue_metadata(row)["review_approval"]
        assert isinstance(approval, dict)
        assert approval["reviewed_sha"] == ROW_HEAD
        assert approval["approver_kind"] == "operator"

    def test_unresolved_login_records_empty_actor(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        patch_row_head(monkeypatch)
        monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
        _seed()
        with caplog.at_level(logging.WARNING):
            approve_ticket(ACME_TICKET, ACME)
        assert _approved_events()[0]["actor"] == ""
        assert any(
            "review-less release for acme/ACM-1: operator login unresolved"
            in r.getMessage()
            for r in caplog.records
        )


class TestFailClosed:
    def test_unresolvable_head_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_row_head(monkeypatch, None)
        _seed()
        _assert_refused_unchanged(_queue_bytes(), "worktree HEAD could not be read")

    def test_non_oserror_from_git_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _seed(worktree_path=worktree)
        before = _queue_bytes()

        def _raise(*_a: object, **_kw: object) -> None:
            msg = "git timed out"
            raise GitTimeoutError(msg)

        monkeypatch.setattr("cw.branch_ahead._run_git", _raise)
        with pytest.raises(GitTimeoutError):
            approve_ticket(ACME_TICKET, ACME)
        assert _queue_bytes() == before
        assert _approved_events() == []

    def test_event_failure_leaves_no_stamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_row_head(monkeypatch)
        _seed()
        before = _queue_bytes()

        def _boom(*_a: object, **_kw: object) -> None:
            msg = "event log unwritable"
            raise OSError(msg)

        monkeypatch.setattr("cw.dev_queue.review_less_release.record_event", _boom)
        with pytest.raises(OSError, match="event log unwritable"):
            approve_ticket(ACME_TICKET, ACME)
        assert _queue_bytes() == before
        assert queue_row().review_approved_at is None

    def test_save_failure_logs_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        patch_row_head(monkeypatch)
        _seed()
        before = _queue_bytes()

        def _boom(*_a: object, **_kw: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.dev_queue.review_less_release.save_dev_queue", _boom)
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(OSError, match="disk full"),
        ):
            approve_ticket(ACME_TICKET, ACME)
        assert _queue_bytes() == before
        assert len(_approved_events()) == 1
        assert any(
            "review-less release save failed" in r.message for r in caplog.records
        )


class TestTerminalRefusalKept:
    def test_automatic_caller_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_row_head(monkeypatch)
        _seed()
        before = _queue_bytes()
        with _lock(), pytest.raises(ApproveGateError, match="terminal stage"):
            _approve_ticket_locked(ACME_TICKET, ACME, operator_initiated=False)
        assert _queue_bytes() == before
        assert _approved_events() == []

    def test_lane_with_review_keeps_the_review_requeue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_lane_client(ACME, tmp_path / "ws")
        patch_row_head(monkeypatch)
        _seed()
        with pytest.raises(ApproveGateError, match="terminal stage") as exc:
            approve_ticket(ACME_TICKET, ACME)
        assert cmd("requeue", "--stage review") in str(exc.value)
        assert queue_row().review_approved_at is None

    @pytest.mark.parametrize(
        "last_result",
        [
            pytest.param(_STALE_TIP, id="stale-tip"),
            pytest.param(REVIEW_PENDING, id="no-park"),
        ],
    )
    def test_other_finalize_rows_are_refused(
        self, monkeypatch: pytest.MonkeyPatch, last_result: dict[str, object]
    ) -> None:
        patch_row_head(monkeypatch)
        _seed(last_result=last_result)
        _assert_refused_unchanged(_queue_bytes(), "terminal stage")

    def test_raise_if_terminal_stage_is_raise_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R3: the shared guard never releases, even for an approval park."""
        patch_row_head(monkeypatch)
        task = _seed()
        before = _queue_bytes()
        session = bound_session_for(task, last_result=STEP40_PARK)
        with pytest.raises(ApproveGateError, match="terminal stage") as exc:
            # [plan, impl, finalize] is the TERMINAL_NO_REVIEW lane for FINALIZE.
            _raise_if_terminal_stage(
                ACME_TICKET,
                session,
                task,
                [Stage.PLAN, Stage.IMPL, Stage.FINALIZE],
            )
        assert cmd("requeue", "--stage finalize") in str(exc.value)
        assert task.review_approved_at is None
        assert _queue_bytes() == before
        assert _approved_events() == []
