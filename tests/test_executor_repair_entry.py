"""Tests for cw.executor._repair_entry: the repair-entry guard (#2944, RFC 0018 B1).

The OpenCode and local IMPL executors do not read ``review_feedback``. The local
executor refuses an adopted repair row (feedback and branch). The OpenCode
executor refuses any IMPL row carrying feedback (``block_any_feedback=True``),
because #2983 decided fail-closed for opencode (RFC 0018 B1, #3059). Either
refusal happens in pre-flight, before any agent launches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

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
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.models.review_feedback import ReviewFeedback
from tests._opencode_helpers import blocked_result

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


def _flags(backend: str) -> dict[str, bool]:
    """The keyword the real call site passes: opencode refuses any feedback row."""
    return {"block_any_feedback": True} if backend == OPENCODE_BACKEND else {}


def test_repair_row_is_recognised_only_with_feedback_and_adoption_branch() -> None:
    assert is_adopted_repair_entry(_repair_task()) is True
    assert is_adopted_repair_entry(_repair_task(branch=None)) is False
    assert is_adopted_repair_entry(_repair_task(review_feedback=None)) is False


@pytest.mark.parametrize("backend", _BACKENDS)
@pytest.mark.parametrize("stage", _STAGES, ids=[s.value for s in _STAGES])
def test_only_an_impl_repair_row_is_refused(backend: str, stage: Stage) -> None:
    details = repair_entry_block_details(
        _repair_task(), stage, backend, **_flags(backend)
    )
    if stage == Stage.IMPL:
        assert details is not None
        assert backend in details
        assert "#2983" in details
    else:
        assert details is None


def test_local_plain_impl_row_with_feedback_is_not_refused() -> None:
    """Kill-check: local stays byte-identical; a no-branch feedback row passes."""
    task = _repair_task(branch=None)
    assert repair_entry_block_details(task, Stage.IMPL, LOCAL_BACKEND) is None


def test_opencode_flag_blocks_feedback_row_without_branch() -> None:
    task = _repair_task(branch=None)
    details = repair_entry_block_details(
        task, Stage.IMPL, OPENCODE_BACKEND, block_any_feedback=True
    )
    assert details is not None
    assert OPENCODE_BACKEND in details


def test_opencode_flag_still_requires_feedback_and_impl() -> None:
    no_feedback = _repair_task(review_feedback=None)
    for stage in (Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE):
        assert (
            repair_entry_block_details(
                no_feedback, stage, OPENCODE_BACKEND, block_any_feedback=True
            )
            is None
        )
    for stage in (Stage.PLAN, Stage.REVIEW, Stage.FINALIZE):
        assert (
            repair_entry_block_details(
                _repair_task(), stage, OPENCODE_BACKEND, block_any_feedback=True
            )
            is None
        )


def test_reason_literal() -> None:
    assert REPAIR_ENTRY_UNSUPPORTED_BACKEND == "repair_entry_unsupported_backend"


def _client(workspace: Path) -> ClientConfig:
    return ClientConfig(name="test", workspace_path=workspace)


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
    result = blocked_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == REPAIR_ENTRY_UNSUPPORTED_BACKEND
    assert result.blocker.details is not None
    assert OPENCODE_BACKEND in result.blocker.details


def test_opencode_refuses_feedback_row_without_branch_before_launch(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A plain impl row with feedback (no adoption branch) is refused on opencode.

    ``opencode_available`` is not patched: the guard must precede the binary probe.
    """
    worktree = make_git_repo("wt-opencode-feedback-no-branch")
    fake_runner = FakeFireAndForgetRunner()
    executor = OpencodeExecutor(
        config=StageExecutorConfig(backend=OPENCODE_BACKEND, model="m"),
        runner=fake_runner,
    )

    executor.spawn(
        stage=Stage.IMPL,
        task=_repair_task(branch=None),
        worktree=worktree,
        client=_client(worktree),
    )

    assert len(fake_runner.calls) == 0
    result = blocked_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == REPAIR_ENTRY_UNSUPPORTED_BACKEND
    assert result.blocker.details is not None
    assert OPENCODE_BACKEND in result.blocker.details
    assert result.stage_reached == "stage2_impl"


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
    result = blocked_result()
    assert result.blocker is not None
    assert result.blocker.reason == REPAIR_ENTRY_UNSUPPORTED_BACKEND
