"""``cw dev-queue hold`` CLI: refusals, conversion, repeat and watch (#2321)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
    WatchedPr,
)
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    events_of,
    install_acme_client,
    invoke_dev_queue,
    make_task,
    queue_row,
    seed_queue,
)
from tests._tool_fakes import FakeGhCli

_HOLD_ARGS = ("hold", ACME_TICKET, "--until-pr-merged", "9", "-c", ACME)
_OPEN_PR = json.dumps({"state": "OPEN", "url": "https://github.com/foo/bar/pull/9"})


@pytest.fixture
def repo_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run outside any dispatch worktree; origin resolves to ``foo/bar``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "cw.cli.dev_queue.hold._resolve_repo_slug", lambda _git_dir: "foo/bar"
    )


def _watches() -> list[WatchedPr]:
    return list(load_dev_queue().watched_prs)


def test_happy_path_holds_pending_row_and_registers_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    row = queue_row()
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.disposition == DEPENDENCY_HOLD_DISPOSITION
    assert row.blocked_reason == "dependency_unmerged"
    assert row.blocked_on_pr == 9
    assert row.session_id is None
    assert "Held ACM-1 in acme dev-queue until PR #9 merges" in result.output
    # The echoed row exposes the PR the hold waits on (#2321 R7).
    assert "blocked_on_pr: 9" in result.output
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.correlation_id == ACME_TICKET
    assert held.payload["outcome"] == "held"
    assert held.payload["pr_number"] == 9
    assert held.payload["repo"] == "foo/bar"
    [watch] = _watches()
    assert watch.source == "dependency_hold"
    assert watch.client == ACME
    assert watch.pr_number == 9
    assert len(fake.records) == 1


def test_lock_discipline_one_gh_call_outside_any_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    assert invoke_dev_queue(*_HOLD_ARGS).exit_code == 0
    assert all(snapshot == () for snapshot in fake.lock_snapshots)
    assert len(fake.records) == 1


def test_refuses_row_that_already_has_a_pr_and_leaves_it_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue(
        [
            make_task(
                status=QueueItemStatus.BLOCKED_ON_USER,
                disposition="merge_pending",
                blocked_reason="automerge_not_armed",
                pr_url="https://github.com/foo/bar/pull/5",
            )
        ]
    )
    before = queue_row().model_dump()
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "row already has PR #5; a hold applies before a PR exists" in result.output
    assert queue_row().model_dump() == before
    assert fake.records == []
    assert _watches() == []


@pytest.mark.parametrize(
    "session_status",
    [SessionStatus.ACTIVE, SessionStatus.IDLE, SessionStatus.BACKGROUNDED],
)
def test_refuses_row_bound_to_live_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
    session_status: SessionStatus,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    task = make_task(status=QueueItemStatus.BLOCKED_ON_USER, session_id="sess-1")
    seed_queue([task], [bound_session_for(task, status=session_status)])
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "close the session first" in result.output
    assert "cw spawn close" in result.output
    assert queue_row().session_id == "sess-1"
    assert fake.records == []
    assert _watches() == []


def test_worker_park_is_converted_to_operator_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    task = make_task(
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition=DEPENDENCY_HOLD_DISPOSITION,
        blocked_reason="dependency_unmerged",
        blocked_on_pr=9,
        session_id="sess-1",
    )
    seed_queue([task], [bound_session_for(task, status=SessionStatus.COMPLETED)])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    assert "updated (converted from worker park)" in result.output
    assert "unchanged" not in result.output
    row = queue_row()
    assert row.session_id is None
    assert row.blocked_on_pr == 9
    assert len(fake.records) == 1
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.payload["outcome"] == "updated"
    assert held.payload["converted_from_worker_park"] is True
    assert held.payload["cleared_session_id"] == "sess-1"


def test_repeat_on_operator_hold_is_unchanged_with_zero_gh_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue(
        [
            make_task(
                status=QueueItemStatus.BLOCKED_ON_USER,
                disposition=DEPENDENCY_HOLD_DISPOSITION,
                blocked_reason="dependency_unmerged",
                blocked_on_pr=9,
            )
        ]
    )
    before = queue_row().model_dump()
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output
    assert "blocked_on_pr: 9" in result.output
    assert queue_row().model_dump() == before
    assert fake.records == []
    assert _watches() == []
    assert events_of(OrchestratorEventType.TICKET_HELD) == []


@pytest.mark.parametrize(
    ("pr_view", "message"),
    [
        (
            json.dumps({"state": "MERGED", "url": "https://github.com/foo/bar/pull/9"}),
            "already merged; requeue instead",
        ),
        (
            json.dumps({"state": "CLOSED", "url": "https://github.com/foo/bar/pull/9"}),
            "closed unmerged",
        ),
        (
            json.dumps(
                {"state": "OPEN", "url": "https://github.com/other/repo/pull/9"}
            ),
            "is not in client acme's repo foo/bar",
        ),
    ],
)
def test_refuses_pr_that_is_merged_closed_or_cross_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
    pr_view: str,
    message: str,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    fake = FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=pr_view)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert message in result.output
    assert queue_row().status == QueueItemStatus.PENDING
    assert _watches() == []
    assert len(fake.records) == 1


def test_refuses_unverifiable_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])
    FakeGhCli().install(monkeypatch).reply("pr", "view", returncode=1, stderr="nope")

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "could not verify PR #9" in result.output
    assert queue_row().status == QueueItemStatus.PENDING


def test_refuses_running_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id=None)])
    fake = FakeGhCli().install(monkeypatch)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "RUNNING" in result.output
    assert queue_row().status == QueueItemStatus.RUNNING
    assert fake.records == []


def test_collision_with_another_client_refused_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    save_dev_queue(
        DevQueueStore(
            tasks=[make_task()],
            watched_prs=[
                WatchedPr(
                    pr_url="https://github.com/foo/bar/pull/9",
                    repo="foo/bar",
                    pr_number=9,
                    source="cli",
                    client="other",
                )
            ],
        )
    )
    FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code != 0
    assert "already watched by client other" in result.output
    assert queue_row().status == QueueItemStatus.PENDING
    assert [w.client for w in _watches()] == ["other"]


def test_previous_disposition_is_reported_when_overwriting_a_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo_slug: None,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue(
        [
            make_task(
                status=QueueItemStatus.BLOCKED_ON_USER,
                disposition="awaiting_operator",
                blocked_reason="agent_block",
            )
        ]
    )
    FakeGhCli().install(monkeypatch).reply("pr", "view", stdout=_OPEN_PR)

    result = invoke_dev_queue(*_HOLD_ARGS)

    assert result.exit_code == 0, result.output
    assert (
        "replaced disposition awaiting_operator, blocked_reason agent_block"
        in result.output
    )
    assert queue_row().disposition == DEPENDENCY_HOLD_DISPOSITION
    # A same-status overwrite emits no task.transition; ticket.held is the
    # durable audit record of what it replaced.
    assert events_of(OrchestratorEventType.TASK_TRANSITION) == []
    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.payload["previous_disposition"] == "awaiting_operator"
    assert held.payload["previous_blocked_reason"] == "agent_block"


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_until_pr_merged_must_be_a_positive_integer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    seed_queue([make_task()])

    result = invoke_dev_queue("hold", ACME_TICKET, "--until-pr-merged", bad, "-c", ACME)

    assert result.exit_code == 2
