"""CLI end-to-end: ``cw dev-queue record-plan-decisions`` (#2984).

The plan stage (Step 1g.1) records the approved plan's ``## Adopted
Assumptions``, ``## Non-Goals`` and ``## Decisions`` on the ticket's row as
decided constraints. The command is run by the headless plan-stage worker, so
it is not worker-guarded; the row-stage check in the recorder is its guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.config import dev_queue_file
from cw.dev_queue.operator_constraints import MAX_CONSTRAINT_BYTES
from cw.models import OrchestratorEventType, Stage
from cw.models.operator_constraint import PLAN_ACTOR
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context
from tests._loop_identity_helpers import (
    COMMIT_A,
    COMMIT_B,
    patch_checkout_identity,
    write_loop_record,
)
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    events_of,
    install_acme_client,
    make_task,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

_PLAN = (
    "# Plan\n\n"
    "## Summary\n\nNot recorded.\n\n"
    "## Adopted Assumptions\n\n- assume A\n\n"
    "## Non-Goals\n\n- Do not add a store.\n- Do not bump the schema.\n\n"
    "## Decisions\n\n- keep X\n\n"
    "## Ambiguities\n\nNO_AMBIGUITIES\n"
)
_EVENT = OrchestratorEventType.OPERATOR_CONSTRAINT_RECORDED


@pytest.fixture(autouse=True)
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed(stage: Stage = Stage.PLAN) -> None:
    seed_queue([make_task(stage=stage)])


def _invoke(runner: CliRunner, tmp_path: Path, plan: str | None = _PLAN) -> Result:
    path = tmp_path / "plan.md"
    if plan is not None:
        path.write_text(plan, encoding="utf-8")
    return runner.invoke(
        main,
        [
            "dev-queue",
            "record-plan-decisions",
            ACME_TICKET,
            "-c",
            ACME,
            "--plan",
            str(path),
        ],
    )


def _kinds_and_texts() -> list[tuple[str, str]]:
    return [(e.kind, e.text) for e in queue_row().operator_constraints]


class TestRecords:
    def test_records_all_three_kinds(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert _kinds_and_texts() == [
            ("adopted_assumption", "assume A"),
            ("non_goal", "Do not add a store."),
            ("non_goal", "Do not bump the schema."),
            ("plan_decision", "keep X"),
        ]
        assert {e.actor for e in queue_row().operator_constraints} == {PLAN_ACTOR}
        assert "Recorded 4 plan decision(s)" in result.stderr
        assert result.stdout == ""
        assert len(events_of(_EVENT)) == 4

    def test_second_run_records_nothing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        _invoke(runner, tmp_path)
        queue_bytes = dev_queue_file().read_bytes()
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert "Recorded 0 plan decision(s)" in result.stderr
        assert "4 already recorded" in result.stderr
        assert dev_queue_file().read_bytes() == queue_bytes
        assert len(events_of(_EVENT)) == 4

    def test_replan_appends_only_the_reworded_bullet(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        _invoke(runner, tmp_path)
        reworded = _PLAN.replace("- keep X", "- keep X and Y")
        result = _invoke(runner, tmp_path, reworded)
        assert result.exit_code == 0, result.output
        assert _kinds_and_texts()[-2:] == [
            ("plan_decision", "keep X"),
            ("plan_decision", "keep X and Y"),
        ]
        assert len(queue_row().operator_constraints) == 5

    def test_plan_without_decided_sections_records_nothing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        result = _invoke(runner, tmp_path, "# Plan\n\n## Summary\n\n- x\n")
        assert result.exit_code == 0, result.output
        assert "no decided sections" in result.stderr
        assert queue_row().operator_constraints == []

    def test_headless_plan_worker_is_not_refused(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _write_session_context(tmp_path, headless=True)
        _seed()
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 0, result.output
        assert len(queue_row().operator_constraints) == 4


class TestRefusals:
    def test_missing_plan_exits_one(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        result = _invoke(runner, tmp_path, None)
        assert result.exit_code == 1
        assert "Cannot read plan file" in result.stderr
        assert queue_row().operator_constraints == []

    def test_non_utf8_plan_exits_one(self, runner: CliRunner, tmp_path: Path) -> None:
        _seed()
        path = tmp_path / "plan.md"
        path.write_bytes(b"## Non-Goals\n\n- \xff\n")
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "record-plan-decisions",
                ACME_TICKET,
                "-c",
                ACME,
                "--plan",
                str(path),
            ],
        )
        assert result.exit_code == 1
        assert "not valid UTF-8" in result.stderr

    @pytest.mark.parametrize("stage", [Stage.REVIEW, Stage.IMPL])
    def test_row_outside_plan_stage_exits_one_with_no_writes(
        self, runner: CliRunner, tmp_path: Path, stage: Stage
    ) -> None:
        _seed(stage)
        queue_bytes = dev_queue_file().read_bytes()
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert "only while the row is in the plan stage" in " ".join(
            result.stderr.split()
        )
        assert dev_queue_file().read_bytes() == queue_bytes
        assert events_of(_EVENT) == []

    def test_missing_row_exits_one(self, runner: CliRunner, tmp_path: Path) -> None:
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert "No dev-queue task found" in result.stderr
        assert events_of(_EVENT) == []

    def test_stale_loop_exits_one_with_plan_wording(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        write_loop_record(commit=COMMIT_A)
        patch_checkout_identity(monkeypatch, commit=COMMIT_B)
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert "Cannot safely record plan decisions" in result.stderr
        assert "operator constraint" not in result.stderr
        assert queue_row().operator_constraints == []

    def test_unknown_loop_identity_exits_one(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        write_loop_record(with_identity=False)
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        assert "Cannot safely record plan decisions" in result.stderr
        assert queue_row().operator_constraints == []

    def test_queue_write_failure_exits_one_with_a_rerun_hint(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "No space left on device"
            raise OSError(msg)

        monkeypatch.setattr("cw.dev_queue.operator_constraints.save_dev_queue", _boom)
        result = _invoke(runner, tmp_path)
        assert result.exit_code == 1
        stderr = " ".join(result.stderr.split())
        assert "writing the dev-queue failed (OSError: No space left on device)" in (
            stderr
        )
        assert "already-recorded items are skipped" in stderr
        assert queue_row().operator_constraints == []

    def test_constrain_keeps_its_operator_wording(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        write_loop_record(commit=COMMIT_A)
        patch_checkout_identity(monkeypatch, commit=COMMIT_B)
        path = tmp_path / "c.md"
        path.write_text("No new lock files.", encoding="utf-8")
        result = runner.invoke(
            main, ["review", "constrain", ACME_TICKET, "--file", str(path), "-c", ACME]
        )
        assert result.exit_code == 1
        assert "Cannot safely record an operator constraint" in result.stderr


class TestCap:
    def test_overflow_exits_three_and_lists_the_unrecorded(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _seed()
        big = "b" * (MAX_CONSTRAINT_BYTES + 1)
        plan = f"## Non-Goals\n\n- first\n- {big}\n- after\n"
        result = _invoke(runner, tmp_path, plan)
        assert result.exit_code == 3, result.output
        assert _kinds_and_texts() == [("non_goal", "first")]
        stderr = result.stderr
        assert "Recorded 1 plan decision(s)" in stderr
        assert "2 plan decision(s) were not recorded" in stderr
        assert "per-entry cap" in stderr
        assert "- [non_goal] after" in stderr
        assert f"- [non_goal] {'b' * 20}" in stderr
