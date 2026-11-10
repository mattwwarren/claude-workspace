"""Stage threading through ``cw codex run``, driver, argv and session purpose (#3054).

The CLI is driven with its runner patched: these tests pin which stages reach a
runner, and that an unserved stage refuses before the debt filer is registered.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cw.cli.codex import _stage_runners, codex_group
from cw.codex_driver import unsupported_codex_stage_message
from cw.codex_runner import build_codex_run_argv
from cw.executor.codex import CODEX_SUPPORTED_STAGES
from cw.executor.core import _create_executor_session, purpose_for_stage
from cw.models import ClientConfig, SessionPurpose, Stage, TicketTask
from cw.models.enums import WORKER_PURPOSES


def _invoke_run(stage: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        codex_group,
        ["run", "T-stage", "--stage", stage, "--session-id", "sid-x"],
    )
    return result.exit_code, result.output


class TestCliStageDispatch:
    def test_review_reaches_runner(self) -> None:
        runner = MagicMock()
        with (
            patch("cw.cli.codex.run_codex_review_stage", runner),
            patch("cw.cli.codex.register_debt_filer") as register,
        ):
            exit_code, _ = _invoke_run("review")
        assert exit_code == 0
        runner.assert_called_once_with(
            ticket_id="T-stage", session_id="sid-x", wall_clock_budget_seconds=None
        )
        register.assert_called_once()

    @pytest.mark.parametrize("stage", ["harden", "plan", "finalize"])
    def test_unserved_stage_refuses_before_runner_and_filer(self, stage: str) -> None:
        runner = MagicMock()
        with (
            patch("cw.cli.codex.run_codex_review_stage", runner),
            patch("cw.cli.codex.register_debt_filer") as register,
        ):
            exit_code, output = _invoke_run(stage)
        assert exit_code != 0
        assert "no codex executor for stage" in output
        assert f"'{stage}'" in output
        runner.assert_not_called()
        register.assert_not_called()

    def test_impl_still_names_ticket_1550(self) -> None:
        runner = MagicMock()
        with patch("cw.cli.codex.run_codex_review_stage", runner):
            exit_code, output = _invoke_run("impl")
        assert exit_code != 0
        assert "#1550" in output
        runner.assert_not_called()

    def test_runner_map_keys_equal_supported_stages(self) -> None:
        assert set(_stage_runners()) == CODEX_SUPPORTED_STAGES


class TestDriverMessages:
    def test_impl_message_keeps_ticket_reference(self) -> None:
        assert "#1550" in unsupported_codex_stage_message(Stage.IMPL)

    @pytest.mark.parametrize("stage", [Stage.HARDEN, Stage.PLAN, Stage.FINALIZE])
    def test_other_stages_name_the_supported_ones(self, stage: Stage) -> None:
        message = unsupported_codex_stage_message(stage)
        assert f"'{stage.value}'" in message
        assert "supported: review" in message


class TestRunnerArgv:
    def test_default_stage_is_review(self) -> None:
        argv = build_codex_run_argv(
            ticket_id="T", session_id="s", wall_clock_budget_seconds=None
        )
        assert argv[argv.index("--stage") + 1] == "review"

    def test_explicit_stage_lands_after_stage_flag(self) -> None:
        argv = build_codex_run_argv(
            ticket_id="T", session_id="s", wall_clock_budget_seconds=None, stage="impl"
        )
        assert argv[argv.index("--stage") + 1] == "impl"


class TestPurposeForStage:
    @pytest.mark.parametrize(
        ("stage", "purpose"),
        [
            (Stage.HARDEN, SessionPurpose.IMPL),
            (Stage.PLAN, SessionPurpose.PLAN),
            (Stage.IMPL, SessionPurpose.IMPL),
            (Stage.REVIEW, SessionPurpose.REVIEW),
            (Stage.FINALIZE, SessionPurpose.FINALIZE),
        ],
    )
    def test_maps_every_stage(self, stage: Stage, purpose: SessionPurpose) -> None:
        assert purpose_for_stage(stage) is purpose

    def test_stage_purposes_stay_out_of_worker_purposes(self) -> None:
        for purpose in (
            SessionPurpose.PLAN,
            SessionPurpose.REVIEW,
            SessionPurpose.FINALIZE,
        ):
            assert purpose not in WORKER_PURPOSES


class TestCreateExecutorSession:
    def _create(self, tmp_path: Path, purpose: SessionPurpose | None) -> SessionPurpose:
        client = ClientConfig(
            name="test", workspace_path=tmp_path, default_branch="main"
        )
        task = TicketTask(ticket_id="T-create", client="test", stage=Stage.REVIEW)
        if purpose is None:
            sess = _create_executor_session(
                task=task, worktree=tmp_path, client=client, stage=Stage.REVIEW
            )
        else:
            sess = _create_executor_session(
                task=task,
                worktree=tmp_path,
                client=client,
                stage=Stage.REVIEW,
                purpose=purpose,
            )
        return sess.purpose

    def test_default_purpose_is_impl(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        assert self._create(tmp_path, None) is SessionPurpose.IMPL

    def test_explicit_purpose_is_recorded(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        assert self._create(tmp_path, SessionPurpose.REVIEW) is SessionPurpose.REVIEW
