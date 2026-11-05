"""CLI tests for ``cw dev-queue add --adopt-pr`` and ``requeue --repin-adopted-head``.

RFC 0016 (#2943). gh is stubbed at ``cw.adopt_pr.fetch_pr_view`` with the
captured ``gh pr view`` fixtures; the dev queue and clients.yaml are real.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import (
    ADOPT_PR_NOT_GREEN_DISPOSITION,
    ADOPTED_HEAD_DRIFT_DISPOSITION,
    load_dev_queue,
    save_dev_queue,
)
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
)
from tests._adopt_pr_helpers import (
    FIXTURE_CONFLICTING,
    FIXTURE_FAILING_CLOSED,
    FIXTURE_GREEN_UNKNOWN,
    load_adopt_fixture,
    make_adopted_task,
)
from tests._clients_yaml import write_clients_yaml
from tests._ticket_view_helpers import invoke_dev_queue, patch_roster
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from click.testing import Result

_CLIENT = "test-client"
_PR = "2933"
_URL = "https://github.com/mattwwarren/claude-workspace/pull/2933"
_HEAD = "bb649bbc53cd4972d5c8d4c50b11030ae3ab57aa"
_BRANCH = "feat/review-feedback-context"


@pytest.fixture(autouse=True)
def _client(tmp_path: Path) -> ClientConfig:
    client = ClientConfig(name=_CLIENT, workspace_path=tmp_path / "ws")
    write_clients_yaml(client, ensure_workspaces=True)
    return client


def _stub_pr(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object] | None
) -> list[str]:
    calls: list[str] = []

    def _fake(pr_ref: str, **_kw: object) -> dict[str, object] | None:
        calls.append(pr_ref)
        return payload

    monkeypatch.setattr("cw.adopt_pr.fetch_pr_view", _fake)
    return calls


def _green() -> dict[str, object]:
    # mutated from capture: mergeable
    return load_adopt_fixture(FIXTURE_GREEN_UNKNOWN, mergeable="MERGEABLE")


def _adopt(*extra: str) -> Result:
    return invoke_dev_queue("add", _PR, "--adopt-pr", _PR, "-c", _CLIENT, *extra)


class TestAdd:
    def test_adopts_one_row_and_echoes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_pr(monkeypatch, _green())
        result = _adopt()
        assert result.exit_code == 0, result.output
        assert f"Adopted {_URL} as {_PR}" in result.stdout
        assert "(draft; finalize marks it ready)" in result.stdout
        (task,) = load_dev_queue().tasks
        assert task.status == QueueItemStatus.PENDING
        assert (task.branch, task.adopted_head_sha, task.adopted_pr_url) == (
            _BRANCH,
            _HEAD,
            _URL,
        )
        (event,) = read_events(event_types=[OrchestratorEventType.TICKET_ENQUEUED])
        assert event.payload["adopted_pr_url"] == _URL
        assert event.payload["branch"] == _BRANCH
        assert event.payload["adopted_head_sha"] == _HEAD
        assert event.payload["stage"] == "review"

    def test_explicit_review_stage_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_pr(monkeypatch, _green())
        assert _adopt("--stage", "review").exit_code == 0

    def test_multiple_tickets_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_pr(monkeypatch, _green())
        result = invoke_dev_queue("add", "1", "2", "--adopt-pr", _PR, "-c", _CLIENT)
        assert result.exit_code == 2
        assert "exactly one TICKET" in result.output
        assert calls == []

    def test_non_review_stage_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_pr(monkeypatch, _green())
        result = _adopt("--stage", "impl")
        assert result.exit_code == 2
        assert "enters at review" in result.output

    def test_fork_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # mutated from capture: isCrossRepository
        _stub_pr(monkeypatch, load_adopt_fixture(isCrossRepository=True))
        result = _adopt()
        assert result.exit_code == 1
        assert "fork PRs" in result.output
        assert load_dev_queue().tasks == []

    def test_closed_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_pr(monkeypatch, load_adopt_fixture(FIXTURE_FAILING_CLOSED))
        result = invoke_dev_queue("add", "2777", "--adopt-pr", "2777", "-c", _CLIENT)
        assert result.exit_code == 1
        assert "not OPEN" in result.output

    def test_live_row_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    make_adopted_task(
                        ticket_id="OTHER",
                        client=_CLIENT,
                        adopted_pr_url=_URL,
                        branch=_BRANCH,
                    )
                ]
            )
        )
        _stub_pr(monkeypatch, _green())
        result = _adopt()
        assert result.exit_code == 1
        assert "already carried by live row 'OTHER'" in result.output

    def test_existing_ticket_row_refused_distinctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id=_PR, client=_CLIENT, status=QueueItemStatus.CANCELLED
                    )
                ]
            )
        )
        _stub_pr(monkeypatch, _green())
        result = _adopt()
        assert result.exit_code == 1
        assert "already has a row" in result.output
        assert "already queued" not in result.output

    def test_parked_intake_shows_the_disposition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_pr(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        result = _adopt()
        assert result.exit_code == 0, result.output
        assert f"parked {ADOPT_PR_NOT_GREEN_DISPOSITION}" in result.stdout
        (task,) = load_dev_queue().tasks
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        (event,) = read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        assert event.payload["paused_status"] == ADOPT_PR_NOT_GREEN_DISPOSITION
        assert event.payload["adopted_head_sha"] == _HEAD
        assert event.payload["pr_url"] == _URL

    def test_unverified_tracker_prints_a_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_pr(monkeypatch, _green())
        result = invoke_dev_queue("add", "ABC-1", "--adopt-pr", _PR, "-c", _CLIENT)
        assert result.exit_code == 0, result.output
        assert "existence not verified" in result.stderr

    def test_help_names_the_flags(self) -> None:
        assert "--adopt-pr" in invoke_dev_queue("add", "--help").output
        assert "--repin-adopted-head" in invoke_dev_queue("requeue", "--help").output


def _seed_park(disposition: str) -> None:
    save_dev_queue(
        DevQueueStore(
            tasks=[
                make_adopted_task(
                    ticket_id=_PR,
                    client=_CLIENT,
                    branch=_BRANCH,
                    adopted_head_sha="a" * 40,
                    adopted_pr_url=_URL,
                    pr_url=_URL,
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    disposition=disposition,
                )
            ]
        )
    )


class TestRequeue:
    def test_repin_flag_fetches_then_passes_the_keyword(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(ADOPTED_HEAD_DRIFT_DISPOSITION)
        patch_roster(monkeypatch, set())
        order: list[str] = []

        def _fetch(repo: Path, branch: str) -> None:
            order.append(f"fetch {branch}")

        def _requeue(*_a: object, **kw: object) -> dict[str, object]:
            order.append("requeue")
            assert kw["repin_adopted_head"] is True
            return {
                "from_stage": "review",
                "to_stage": "review",
                "regressed": False,
                "regress_attempts": 0,
                "regress_inferred": False,
                "already_pending": False,
                "attempts_reset": False,
                "prior_unproductive_attempts": 0,
                "attempts_reset_was_capped": False,
                "from_cancelled_applied": False,
                "from_failed_applied": False,
                "from_completed_applied": False,
                "adopted_repinned": True,
                "adopted_head_sha_old": "a" * 40,
                "adopted_head_sha": "c" * 40,
            }

        monkeypatch.setattr("cw.cli.dev_queue._adopt.fetch_adopted_branch", _fetch)
        monkeypatch.setattr("cw.cli.dev_queue.crud.requeue_ticket", _requeue)
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT, "--repin-adopted-head")
        assert result.exit_code == 0, result.output
        assert order == [f"fetch {_BRANCH}", "requeue"]
        assert f"Re-pinned the adopted head {'a' * 40} -> {'c' * 40}." in result.stdout
        (event,) = read_events(event_types=[OrchestratorEventType.TICKET_REQUEUED])
        assert event.payload["adopted_head_sha"] == "c" * 40

    def test_plain_requeue_of_drift_park_refused_with_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(ADOPTED_HEAD_DRIFT_DISPOSITION)
        patch_roster(monkeypatch, set())
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 1
        assert "--repin-adopted-head" in result.output
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_not_green_park_still_red_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(ADOPT_PR_NOT_GREEN_DISPOSITION)
        patch_roster(monkeypatch, set())
        calls = _stub_pr(monkeypatch, load_adopt_fixture(FIXTURE_CONFLICTING))
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 1
        assert "stays parked" in result.output
        assert calls == [_PR]
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_not_green_park_now_green_requeues_and_repins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_park(ADOPT_PR_NOT_GREEN_DISPOSITION)
        patch_roster(monkeypatch, set())
        _stub_pr(monkeypatch, _green())
        result = invoke_dev_queue("requeue", _PR, "-c", _CLIENT)
        assert result.exit_code == 0, result.output
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.adopted_head_sha == _HEAD
