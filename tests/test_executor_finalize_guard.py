"""Tests for cw.executor._finalize_guard: opencode FINALIZE fails closed.

RFC 0018 B1, #3059.

The claude-native ``auto-dev-finalize.md`` runs steps the opencode backend cannot
execute, and no real opencode FINALIZE run has verified it. The guard refuses the
spawn in pre-flight, before the binary probe and before any agent runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import KNOWN_BLOCKER_REASONS
from cw.config import load_state
from cw.executor._finalize_guard import (
    FINALIZE_UNSUPPORTED_BACKEND,
    finalize_block_details,
)
from cw.executor.core import FakeFireAndForgetRunner
from cw.executor.opencode import OpencodeExecutor
from cw.models import (
    OPENCODE_BACKEND,
    ClientConfig,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.opencode_runner import STAGE4A_MERGE_GATE
from tests._opencode_helpers import blocked_result
from tests._shared.session_seeding import find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable

_NON_FINALIZE = (Stage.PLAN, Stage.IMPL, Stage.REVIEW)


def _task() -> TicketTask:
    return TicketTask.model_validate(
        {"ticket_id": "T-1", "client": "test", "stage": Stage.FINALIZE}
    )


def test_reason_literal_and_registry() -> None:
    assert FINALIZE_UNSUPPORTED_BACKEND == "finalize_unsupported_backend"
    assert FINALIZE_UNSUPPORTED_BACKEND in KNOWN_BLOCKER_REASONS


@pytest.mark.parametrize("stage", _NON_FINALIZE, ids=[s.value for s in _NON_FINALIZE])
def test_only_a_finalize_spawn_is_refused(stage: Stage) -> None:
    assert finalize_block_details(stage, OPENCODE_BACKEND) is None


def test_finalize_spawn_is_refused_with_backend_named() -> None:
    details = finalize_block_details(Stage.FINALIZE, OPENCODE_BACKEND)
    assert details is not None
    assert f"backend={OPENCODE_BACKEND}" in details
    assert "#3059" in details


@pytest.mark.parametrize("binary_available", [True, False])
def test_opencode_finalize_refused_before_launch(
    binary_available: bool,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The guard precedes the binary probe, so the reason holds either way."""
    worktree = make_git_repo("wt-opencode-finalize-refused")
    fake_runner = FakeFireAndForgetRunner()
    executor = OpencodeExecutor(
        config=StageExecutorConfig(backend=OPENCODE_BACKEND, model="m"),
        runner=fake_runner,
    )

    with patch(
        "cw.executor.opencode.opencode_available", return_value=binary_available
    ):
        executor.spawn(
            stage=Stage.FINALIZE,
            task=_task(),
            worktree=worktree,
            client=ClientConfig(name="test", workspace_path=worktree),
        )

    result = blocked_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == FINALIZE_UNSUPPORTED_BACKEND
    assert result.blocker.retry_eligible is False
    assert result.stage_reached == STAGE4A_MERGE_GATE
    assert result.blocker.details is not None
    assert OPENCODE_BACKEND in result.blocker.details
    assert len(fake_runner.calls) == 0
    assert find_completed_session(load_state()).status == SessionStatus.COMPLETED


def test_headless_contract_documents_both_unsupported_backend_reasons() -> None:
    contract = (
        Path(__file__).parent.parent / "docs" / "headless-contract.md"
    ).read_text(encoding="utf-8")
    assert "| `finalize_unsupported_backend` |" in contract
    assert "| `repair_entry_unsupported_backend` |" in contract
