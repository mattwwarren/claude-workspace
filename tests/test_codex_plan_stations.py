"""Tests for cw.codex_plan._stations: review stations and markers (#3060 R1)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from cw.codex_plan._stations import (
    PLAN_REVIEWER,
    PLAN_SOUNDNESS_REVIEWER,
    STATION_OUTPUT_INSTRUCTIONS,
    build_station_prompt,
    run_plan_stations,
    stamp_markers,
)
from cw.codex_review import CODEX_MODEL_CAPACITY, PLAN_STATION_AGENT_FILES
from cw.codex_runner import CodexRunResult
from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from tests._codex_review_helpers import _populate_global_agents_dir
from tests._tool_fakes import (
    ScriptedCodexRunner,
    station_findings_doc,
    stdin_contains,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_plan._stations import StationsOutcome

_PLAN = "## Summary\n\nThe plan.\n\n**Scope tier:** small\n"
_TODAY = date(2026, 11, 10)


def _stations(
    runner: ScriptedCodexRunner, worktree: Path, *, binding: bool = True
) -> StationsOutcome:
    return run_plan_stations(
        runner=runner,
        worktree=worktree,
        plan_text=_PLAN,
        model="gpt-plan",
        reasoning_effort="high",
        session_id="sess-st",
        binding=binding,
    )


def _runner(reviewer: CodexRunResult, soundness: CodexRunResult) -> ScriptedCodexRunner:
    return (
        ScriptedCodexRunner()
        .add(stdin_contains(f"You are the {PLAN_SOUNDNESS_REVIEWER}"), soundness)
        .add(stdin_contains(f"You are the {PLAN_REVIEWER}"), reviewer)
    )


def _clean(role: str) -> CodexRunResult:
    return station_findings_doc(role)


class TestVerdicts:
    def test_both_clean_is_clean_and_markers_stamp(self, tmp_path: Path) -> None:
        runner = _runner(_clean(PLAN_REVIEWER), _clean(PLAN_SOUNDNESS_REVIEWER))
        outcome = _stations(runner, tmp_path)
        assert outcome.clean
        assert len(runner.calls) == 2
        stamped = stamp_markers(_PLAN, today=_TODAY)
        assert _plan_body_signoff_ok(stamped)
        lines = stamped.splitlines()
        assert lines[0] == "<!-- plan-spec-reviewed: 2026-11-10 v2 -->"
        assert lines[1] == "<!-- plan-soundness-reviewed: 2026-11-10 v1 -->"

    def test_blocking_reviewer_parks_and_skips_soundness(self, tmp_path: Path) -> None:
        runner = _runner(
            station_findings_doc(PLAN_REVIEWER, severity="MUST_FIX"),
            _clean(PLAN_SOUNDNESS_REVIEWER),
        )
        outcome = _stations(runner, tmp_path)
        assert outcome.block is not None
        assert outcome.block.reason == "plan_unreviewable"
        assert outcome.block.retry_eligible is False
        assert "MUST_FIX finding from Plan Reviewer" in outcome.block.details
        assert len(runner.calls) == 1

    def test_blocking_soundness_reviewer_parks_unsound(self, tmp_path: Path) -> None:
        runner = _runner(
            _clean(PLAN_REVIEWER),
            station_findings_doc(PLAN_SOUNDNESS_REVIEWER, severity="MUST_FIX"),
        )
        outcome = _stations(runner, tmp_path)
        assert outcome.block is not None
        assert outcome.block.reason == "plan_unsound"
        assert "MUST_FIX" in outcome.block.details

    def test_should_fix_only_is_clean(self, tmp_path: Path) -> None:
        runner = _runner(
            station_findings_doc(PLAN_REVIEWER, severity="SHOULD_FIX"),
            _clean(PLAN_SOUNDNESS_REVIEWER),
        )
        outcome = _stations(runner, tmp_path)
        assert outcome.clean
        assert any("SHOULD_FIX" in line for line in outcome.findings)

    @pytest.mark.parametrize("status", ["degraded", "failed"])
    def test_degraded_or_failed_is_not_clean(self, tmp_path: Path, status: str) -> None:
        runner = _runner(
            station_findings_doc(PLAN_REVIEWER, status=status, detail="no ticket"),
            _clean(PLAN_SOUNDNESS_REVIEWER),
        )
        outcome = _stations(runner, tmp_path)
        assert outcome.block is not None
        assert outcome.block.reason == "plan_unreviewable"
        assert status in outcome.block.details
        assert outcome.block.retry_eligible is False

    def test_rejected_item_is_not_clean(self, tmp_path: Path) -> None:
        bad = station_findings_doc(PLAN_SOUNDNESS_REVIEWER, severity="BOGUS")
        runner = _runner(_clean(PLAN_REVIEWER), bad)
        outcome = _stations(runner, tmp_path)
        assert outcome.block is not None
        assert outcome.block.reason == "plan_unsound"
        assert "failed validation" in outcome.block.details

    def test_capacity_failure_is_the_only_retryable_one(self, tmp_path: Path) -> None:
        capacity = CodexRunResult(
            returncode=1,
            stdout=(
                '{"type":"turn.failed","error":{"message":'
                '"Selected model is at capacity. Please try a different model."}}\n'
            ),
            stderr="",
        )
        runner = _runner(capacity, _clean(PLAN_SOUNDNESS_REVIEWER))
        outcome = _stations(runner, tmp_path)
        assert outcome.block is not None
        assert outcome.block.reason == CODEX_MODEL_CAPACITY
        assert outcome.block.retry_eligible is True

    def test_other_failure_is_codex_error(self, tmp_path: Path) -> None:
        crash = CodexRunResult(returncode=2, stdout="", stderr="boom")
        runner = _runner(crash, _clean(PLAN_SOUNDNESS_REVIEWER))
        outcome = _stations(runner, tmp_path)
        assert outcome.block is not None
        assert outcome.block.reason == "codex_error"
        assert outcome.block.retry_eligible is False

    def test_advisory_runs_both_and_never_blocks(self, tmp_path: Path) -> None:
        runner = _runner(
            station_findings_doc(PLAN_REVIEWER, severity="MUST_FIX"),
            station_findings_doc(PLAN_SOUNDNESS_REVIEWER, severity="MUST_FIX"),
        )
        outcome = _stations(runner, tmp_path, binding=False)
        assert outcome.block is None
        assert len(runner.calls) == 2
        assert len(outcome.findings) == 2


class TestLaunch:
    def test_station_argv_is_read_only_with_no_timeout(self, tmp_path: Path) -> None:
        runner = _runner(_clean(PLAN_REVIEWER), _clean(PLAN_SOUNDNESS_REVIEWER))
        _stations(runner, tmp_path)
        for call in runner.calls:
            argv = call["argv"]
            assert isinstance(argv, list)
            sandbox = argv.index("--sandbox")
            assert argv[sandbox : sandbox + 4] == [
                "--sandbox",
                "read-only",
                "-c",
                "approval_policy=never",
            ]
            assert call["timeout"] is None
            assert argv[-2:] == ["-m", "gpt-plan"]

    def test_prompt_never_asks_for_a_marker(self) -> None:
        prompt = build_station_prompt(PLAN_REVIEWER, "SPEC TEXT", _PLAN)
        assert "SPEC TEXT" in prompt
        assert _PLAN.rstrip() in prompt
        assert "plan-spec-reviewed" not in prompt
        assert "marker" not in STATION_OUTPUT_INSTRUCTIONS.lower()

    def test_missing_spec_is_named(self) -> None:
        prompt = build_station_prompt(PLAN_REVIEWER, "  ", _PLAN)
        assert "no agent specification was found" in prompt


class TestSpecResolution:
    def test_repo_spec_wins(self, tmp_path: Path) -> None:
        agents = tmp_path / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "plan-reviewer.md").write_text("REPO REVIEWER SPEC", encoding="utf-8")
        runner = _runner(_clean(PLAN_REVIEWER), _clean(PLAN_SOUNDNESS_REVIEWER))
        _stations(runner, tmp_path)
        stdin = str(runner.calls[0]["stdin"])
        assert "REPO REVIEWER SPEC" in stdin

    def test_global_spec_is_the_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_dir = tmp_path / "global-agents"
        _populate_global_agents_dir(
            global_dir,
            plan_reviewer="GLOBAL REVIEWER",
            plan_soundness_reviewer="GLOBAL SOUNDNESS",
        )
        monkeypatch.setattr(
            "cw.codex_review._context._agent_spec._GLOBAL_AGENTS_DIR", global_dir
        )
        worktree = tmp_path / "wt"
        worktree.mkdir()
        runner = _runner(_clean(PLAN_REVIEWER), _clean(PLAN_SOUNDNESS_REVIEWER))
        _stations(runner, worktree)
        assert "GLOBAL REVIEWER" in str(runner.calls[0]["stdin"])
        assert "GLOBAL SOUNDNESS" in str(runner.calls[1]["stdin"])
        assert set(PLAN_STATION_AGENT_FILES) == {PLAN_REVIEWER, PLAN_SOUNDNESS_REVIEWER}
