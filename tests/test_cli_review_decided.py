"""CLI end-to-end: ``cw review decided`` (#2984).

The read-only renderer of the Claude-native reviewers' ``## Decided
Constraints`` block: every row constraint entry, verbatim, grouped by kind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.config import dev_queue_file
from cw.models import Stage
from cw.models.operator_constraint import DECIDED_SCOPE_INSTRUCTIONS
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    install_acme_client,
    make_task,
    seed_queue,
)
from tests.conftest import _make_constraint

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

    from cw.models.operator_constraint import OperatorConstraint

_NOT_RECORDED = (
    "Note: `.cw/plan.md` has decision sections that were not recorded on this"
    " ticket; treat them as plan context, not as binding decided constraints."
)


@pytest.fixture(autouse=True)
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)


def _seed(*entries: OperatorConstraint) -> None:
    seed_queue([make_task(stage=Stage.REVIEW, operator_constraints=list(entries))])


def _invoke() -> Result:
    return CliRunner().invoke(main, ["review", "decided", ACME_TICKET, "-c", ACME])


def _write_plan(tmp_path: Path, text: str) -> None:
    plan = tmp_path / ".cw" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(text, encoding="utf-8")


class TestOutput:
    def test_groups_entries_by_kind_verbatim(self) -> None:
        _seed(
            _make_constraint("Do not add `foo_bar`."),
            _make_constraint(
                "assume A\n  continued", kind="adopted_assumption", round_=2
            ),
            _make_constraint("not X", kind="non_goal", round_=3),
        )
        result = _invoke()
        assert result.exit_code == 0, result.output
        out = result.stdout
        assert out.startswith("## Decided Constraints\n\n")
        assert DECIDED_SCOPE_INSTRUCTIONS in out
        assert "made on purpose" in DECIDED_SCOPE_INSTRUCTIONS
        assert "contests_decision" in DECIDED_SCOPE_INSTRUCTIONS
        operator = out.index("### Constraint 1 (recorded")
        assumption = out.index("### Decided adopted assumption 2")
        non_goal = out.index("### Decided non-goal 3")
        assert operator < assumption < non_goal
        assert "Do not add `foo_bar`." in out
        assert "assume A\n  continued" in out
        assert "None recorded." not in out

    def test_operator_entries_appear_without_plan_entries(self) -> None:
        _seed(_make_constraint("No new lock files."))
        out = _invoke().stdout
        assert "No new lock files." in out
        assert out.count("None recorded.") == 1

    def test_empty_row_prints_none_recorded(self) -> None:
        _seed()
        result = _invoke()
        assert result.exit_code == 0, result.output
        assert "None recorded." in result.stdout
        assert _NOT_RECORDED not in result.stdout

    def test_missing_row_exits_one(self) -> None:
        seed_queue([])
        result = _invoke()
        assert result.exit_code == 1
        assert "No dev-queue task found" in result.stderr


class TestUnrecordedPlanNotice:
    def test_row_without_plan_entries_prints_the_neutral_notice(
        self, tmp_path: Path
    ) -> None:
        _write_plan(tmp_path, "## Non-Goals\n\n- not X\n")
        _seed(_make_constraint("operator only"))
        queue_bytes = dev_queue_file().read_bytes()
        result = _invoke()
        assert result.exit_code == 0, result.output
        assert _NOT_RECORDED in result.stdout
        assert "record-plan-decisions" not in result.stdout
        assert dev_queue_file().read_bytes() == queue_bytes

    def test_no_notice_when_every_plan_item_is_recorded(self, tmp_path: Path) -> None:
        _write_plan(tmp_path, "## Non-Goals\n\n- not X\n")
        _seed(_make_constraint("not X", kind="non_goal"))
        assert _NOT_RECORDED not in _invoke().stdout

    def test_no_notice_when_the_plan_has_no_decided_sections(
        self, tmp_path: Path
    ) -> None:
        _write_plan(tmp_path, "## Summary\n\n- x\n")
        _seed()
        assert _NOT_RECORDED not in _invoke().stdout

    def test_unreadable_plan_is_treated_as_absent(self, tmp_path: Path) -> None:
        plan = tmp_path / ".cw" / "plan.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_bytes(b"## Non-Goals\n\n- \xff\n")
        _seed()
        result = _invoke()
        assert result.exit_code == 0, result.output
        assert _NOT_RECORDED not in result.stdout


class TestReadOnly:
    def test_headless_worker_is_not_refused_and_nothing_is_written(
        self, tmp_path: Path
    ) -> None:
        _write_session_context(tmp_path, headless=True)
        _seed(_make_constraint("not X", kind="non_goal"))
        queue_bytes = dev_queue_file().read_bytes()
        result = _invoke()
        assert result.exit_code == 0, result.output
        assert "not X" in result.stdout
        assert dev_queue_file().read_bytes() == queue_bytes
