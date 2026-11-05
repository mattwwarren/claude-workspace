"""Tests for cw.executor._repair_entry: the adopted repair-entry guard (#2944).

The OpenCode and local IMPL executors do not read ``review_feedback``, so an
adopted repair row must be refused in pre-flight before any agent launches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.executor._repair_entry import (
    REPAIR_ENTRY_UNSUPPORTED_BACKEND,
    is_adopted_repair_entry,
    repair_entry_block_details,
)
from cw.executor.core import FakeFireAndForgetRunner
from cw.executor.local import LocalExecutor
from cw.executor.opencode import OpencodeExecutor
from cw.models import (
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    ClientConfig,
    LastResultSource,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.models.review_feedback import ReviewFeedback
from tests.conftest import find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable

_SHA = "a" * 40
_BRANCH = "dev/2944"
_STAGES = (Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE)
_BACKENDS = (LOCAL_BACKEND, OPENCODE_BACKEND)


def _feedback() -> ReviewFeedback:
    return ReviewFeedback(
        repo="acme/widgets",
        pr_number=7,
        reviewed_sha=_SHA,
        captured_at=datetime(2026, 11, 5, tzinfo=UTC),
        report="CW PR REPAIR FEEDBACK (test fixture)",
    )


def _repair_task(**overrides: object) -> TicketTask:
    fields: dict[str, object] = {
        "ticket_id": "T-1",
        "client": "test",
        "stage": Stage.IMPL,
        "branch": _BRANCH,
        "review_feedback": _feedback(),
    }
    fields.update(overrides)
    return TicketTask.model_validate(fields)


def test_repair_row_is_recognised_only_with_feedback_and_adoption_branch() -> None:
    assert is_adopted_repair_entry(_repair_task()) is True
    assert is_adopted_repair_entry(_repair_task(branch=None)) is False
    assert is_adopted_repair_entry(_repair_task(review_feedback=None)) is False


@pytest.mark.parametrize("backend", _BACKENDS)
@pytest.mark.parametrize("stage", _STAGES, ids=[s.value for s in _STAGES])
def test_only_an_impl_repair_row_is_refused(backend: str, stage: Stage) -> None:
    details = repair_entry_block_details(_repair_task(), stage, backend)
    if stage == Stage.IMPL:
        assert details is not None
        assert backend in details
        assert "#2983" in details
    else:
        assert details is None


@pytest.mark.parametrize("backend", _BACKENDS)
def test_plain_impl_row_with_feedback_is_not_refused(backend: str) -> None:
    """Kill-check: a manual --stage impl --review-feedback-file row has no branch."""
    task = _repair_task(branch=None)
    assert repair_entry_block_details(task, Stage.IMPL, backend) is None


def test_reason_literal() -> None:
    assert REPAIR_ENTRY_UNSUPPORTED_BACKEND == "repair_entry_unsupported_backend"


def _client(workspace: Path) -> ClientConfig:
    return ClientConfig(name="test", workspace_path=workspace)


def _blocked_result() -> AutoDevResult:
    state = load_state()
    session = find_completed_session(state)
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    return AutoDevResult.model_validate(session.last_result)


def test_opencode_refuses_repair_row_before_launch(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    worktree = make_git_repo("wt-opencode-repair-refused")
    fake_runner = FakeFireAndForgetRunner()
    executor = OpencodeExecutor(
        config=StageExecutorConfig(backend=OPENCODE_BACKEND, model="m"),
        runner=fake_runner,
    )

    executor.spawn(
        stage=Stage.IMPL,
        task=_repair_task(),
        worktree=worktree,
        client=_client(worktree),
    )

    assert len(fake_runner.calls) == 0
    result = _blocked_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == REPAIR_ENTRY_UNSUPPORTED_BACKEND
    assert result.blocker.details is not None
    assert OPENCODE_BACKEND in result.blocker.details


def test_local_refuses_repair_row_before_plan_or_launch(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    worktree = make_git_repo("wt-local-repair-refused")
    fake_runner = FakeFireAndForgetRunner()
    executor = LocalExecutor(
        config=StageExecutorConfig(
            backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
        ),
        runner=fake_runner,
    )

    with patch("cw.executor.local.aider_available", return_value=True):
        executor.spawn(
            stage=Stage.IMPL,
            task=_repair_task(),
            worktree=worktree,
            client=_client(worktree),
        )

    assert len(fake_runner.calls) == 0
    result = _blocked_result()
    assert result.blocker is not None
    assert result.blocker.reason == REPAIR_ENTRY_UNSUPPORTED_BACKEND
