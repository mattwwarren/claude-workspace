"""Tests for cw.plan - plan file parsing and visibility."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from cw.plan import PlanPhase, PlanSummary, PlanTask, find_plan_files, parse_plan

if TYPE_CHECKING:
    from pathlib import Path


class TestFindPlanFiles:
    def test_no_plans_dir(self, tmp_path: Path) -> None:
        result = find_plan_files(tmp_path)
        assert result == []

    def test_empty_plans_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "plans").mkdir(parents=True)
        result = find_plan_files(tmp_path)
        assert result == []

    def test_finds_direct_md_files(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "plan-a.md").write_text("# Plan A\n")
        result = find_plan_files(tmp_path)
        assert len(result) == 1
        assert result[0].name == "plan-a.md"

    def test_finds_nested_md_files(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / ".claude" / "plans" / "my-feature"
        plans_dir.mkdir(parents=True)
        (plans_dir / "main.md").write_text("# Feature Plan\n")
        result = find_plan_files(tmp_path)
        assert len(result) == 1
        assert result[0].name == "main.md"

    def test_sorted_by_mtime_newest_first(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "old.md").write_text("# Old\n")
        time.sleep(0.05)
        (plans_dir / "new.md").write_text("# New\n")
        result = find_plan_files(tmp_path)
        assert len(result) == 2
        assert result[0].name == "new.md"
        assert result[1].name == "old.md"


class TestParsePlan:
    def test_extracts_title(self, tmp_path: Path) -> None:
        f = tmp_path / "plan.md"
        f.write_text("# My Feature Plan\n\n## Phase 1\n\n- [x] Done\n")
        result = parse_plan(f)
        assert result.title == "My Feature Plan"

    def test_fallback_title_from_stem(self, tmp_path: Path) -> None:
        f = tmp_path / "my-plan.md"
        f.write_text("## Phase 1\n\n- [x] Task\n")
        result = parse_plan(f)
        assert result.title == "my-plan"

    def test_extracts_phases(self, tmp_path: Path) -> None:
        f = tmp_path / "plan.md"
        f.write_text(
            "# Plan\n\n"
            "## Phase 1: Setup\n\n"
            "- [x] Create project\n"
            "- [x] Add CI\n\n"
            "## Phase 2: Build\n\n"
            "- [ ] Implement feature\n"
            "- [x] Write tests\n"
        )
        result = parse_plan(f)
        assert len(result.phases) == 2
        assert result.phases[0].name == "Phase 1: Setup"
        assert result.phases[1].name == "Phase 2: Build"

    def test_checkbox_parsing(self, tmp_path: Path) -> None:
        f = tmp_path / "plan.md"
        f.write_text(
            "## Phase 1\n\n"
            "- [x] Completed task\n"
            "- [X] Also completed\n"
            "- [ ] Not done\n"
        )
        result = parse_plan(f)
        tasks = result.phases[0].tasks
        assert len(tasks) == 3
        assert tasks[0].completed is True
        assert tasks[1].completed is True
        assert tasks[2].completed is False

    def test_ignores_checkboxes_outside_phases(self, tmp_path: Path) -> None:
        f = tmp_path / "plan.md"
        f.write_text(
            "# Plan\n\n"
            "- [x] Orphan checkbox\n\n"
            "## Phase 1\n\n"
            "- [ ] Real task\n"
        )
        result = parse_plan(f)
        assert len(result.phases) == 1
        assert len(result.phases[0].tasks) == 1

    def test_no_checkboxes(self, tmp_path: Path) -> None:
        f = tmp_path / "plan.md"
        f.write_text("# Plan\n\n## Phase 1\n\nJust some text.\n")
        result = parse_plan(f)
        assert len(result.phases) == 1
        assert result.phases[0].tasks == []

    def test_empty_plan(self, tmp_path: Path) -> None:
        f = tmp_path / "plan.md"
        f.write_text("")
        result = parse_plan(f)
        assert result.phases == []


class TestPlanPhaseProgress:
    def test_all_done(self) -> None:
        phase = PlanPhase(
            name="P1",
            tasks=[
                PlanTask(text="A", completed=True, phase="P1"),
                PlanTask(text="B", completed=True, phase="P1"),
            ],
        )
        assert phase.progress == (2, 2)

    def test_partial(self) -> None:
        phase = PlanPhase(
            name="P1",
            tasks=[
                PlanTask(text="A", completed=True, phase="P1"),
                PlanTask(text="B", completed=False, phase="P1"),
            ],
        )
        assert phase.progress == (1, 2)

    def test_empty(self) -> None:
        phase = PlanPhase(name="P1")
        assert phase.progress == (0, 0)


class TestPlanSummaryProgress:
    def test_aggregate(self, tmp_path: Path) -> None:
        summary = PlanSummary(
            path=tmp_path / "plan.md",
            title="Plan",
            phases=[
                PlanPhase(
                    name="P1",
                    tasks=[
                        PlanTask(text="A", completed=True, phase="P1"),
                        PlanTask(text="B", completed=True, phase="P1"),
                    ],
                ),
                PlanPhase(
                    name="P2",
                    tasks=[
                        PlanTask(text="C", completed=False, phase="P2"),
                    ],
                ),
            ],
        )
        assert summary.progress == (2, 3)

    def test_empty(self, tmp_path: Path) -> None:
        summary = PlanSummary(path=tmp_path / "plan.md", title="Plan")
        assert summary.progress == (0, 0)
