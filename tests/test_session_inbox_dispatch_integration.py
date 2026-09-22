"""End-to-end coverage for the #2212 send → queue → replay path.

A plain unit test: the daemon and the resume trigger are both faked, so no
tmux/daemon fixture is needed. What it proves is the seam between the CLI
command, the per-session inbox, and the dev-queue row — specifically that
``cw session send`` queues durably without disposing of the queue row, and
that re-applying an already-consumed message moves the cursor file rather
than rewriting the append-only inbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cw import session_inbox
from cw.cli import main
from cw.config import save_state
from cw.dev_queue import list_tickets
from cw.models import (
    CwState,
    DevQueueStore,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.session_resume_trigger import FakeResumeTriggerAdapter
from tests.conftest import _make_daemon_session, _make_ticket_task

if TYPE_CHECKING:
    from cw.models import ClientConfig, Session

_SESSION_ID = "sess2212"
_TICKET = "2212"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _blocked_session(sample_client: ClientConfig) -> Session:
    return _make_daemon_session(
        id=_SESSION_ID,
        name=f"test-client/auto-dev/{_TICKET}",
        client="test-client",
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=sample_client.workspace_path,
        worktree_path=sample_client.workspace_path,
        surface_ref="deadbeef",
        claude_session_id="aaaa1111-0000-0000-0000-000000000000",
        started_at=datetime(2026, 9, 22, 11, 0, 0, tzinfo=UTC),
    )


def _enqueue_blocked_task() -> None:
    """Persist a dev-queue row parked exactly the way the primary path parks it.

    ``transition_task_status`` is the single authority for the transition, so
    the row's ``disposition`` is stamped by production code rather than
    hand-set — that field is what ``queue peek``'s reason string reads.
    """
    from cw.dev_queue import save_dev_queue, transition_task_status

    task = _make_ticket_task(
        ticket_id=_TICKET, client="test-client", session_id=_SESSION_ID
    )
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition="stopped_without_sentinel",
        unproductive=False,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))


def _send(runner: CliRunner, body: str) -> int:
    fake = FakeResumeTriggerAdapter()
    with patch(
        "cw.cli.session_send.get_resume_trigger_adapter", return_value=fake
    ):
        return runner.invoke(
            main, ["session", "send", _SESSION_ID, "--message", body]
        ).exit_code


class TestSendDoesNotDisposeOfTheQueueRow:
    def test_blocked_row_stays_blocked(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """Task-status transition stays a separate, explicit operator action."""
        save_state(CwState(sessions=[_blocked_session(sample_client)]))
        _enqueue_blocked_task()

        assert _send(runner, "use the second approach") == 0

        task = next(t for t in list_tickets("test-client") if t.ticket_id == _TICKET)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.session_id == _SESSION_ID

    def test_queue_peek_surfaces_the_row_as_awaiting_operator(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """The visibility half of #2212, against a real dev-queue row."""
        from cw import queue_peek

        save_state(CwState(sessions=[_blocked_session(sample_client)]))
        _enqueue_blocked_task()

        rows = queue_peek.build_peek_rows("test-client", datetime.now(UTC))

        assert len(rows) == 1
        assert rows[0]["recommend"] == queue_peek.RECOMMEND_AWAITING_OPERATOR
        assert "stopped_without_sentinel" in rows[0]["reason"]


class TestIdempotentReplay:
    def test_consuming_moves_the_cursor_not_the_inbox(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """Closes the 'replay is idempotent' acceptance line: the artifact that
        changes on re-application is the cursor file, never the inbox."""
        save_state(CwState(sessions=[_blocked_session(sample_client)]))
        _enqueue_blocked_task()
        assert _send(runner, "use the second approach") == 0

        inbox = session_inbox.inbox_path(_SESSION_ID)
        inbox_before = inbox.read_bytes()
        pending = session_inbox.read_unconsumed(_SESSION_ID)
        assert [m.body for m in pending] == ["use the second approach"]

        session_inbox.advance_cursor(_SESSION_ID, pending[-1].id)

        assert session_inbox.read_unconsumed(_SESSION_ID) == []
        assert inbox.read_bytes() == inbox_before

    def test_a_second_send_after_consumption_is_the_only_new_work(
        self, sample_client: ClientConfig, runner: CliRunner
    ) -> None:
        """Simulates a `cw spawn close --requeue` + fresh claim re-reading the
        inbox: the consumed message stays on disk but is not re-delivered."""
        save_state(CwState(sessions=[_blocked_session(sample_client)]))
        _enqueue_blocked_task()
        assert _send(runner, "first answer") == 0
        session_inbox.advance_cursor(
            _SESSION_ID, session_inbox.read_unconsumed(_SESSION_ID)[-1].id
        )

        assert _send(runner, "second answer") == 0

        assert [m.body for m in session_inbox.read_messages(_SESSION_ID)] == [
            "first answer",
            "second answer",
        ]
        assert [m.body for m in session_inbox.read_unconsumed(_SESSION_ID)] == [
            "second answer"
        ]
