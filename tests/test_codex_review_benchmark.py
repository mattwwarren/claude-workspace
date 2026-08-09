"""Tests for cw.codex_review._benchmark — the medium-vs-high reasoning-effort
comparison harness (#1711).

No live codex call: the harness drives the real ``run_codex_roles`` against an
injected :class:`CodexRunner` double, so what is exercised is the folding of
``ReviewerFindingsDocument``/``ReviewerRunMetrics`` into the two-sided
comparison, not codex itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.codex_review._benchmark import (
    BenchmarkComparison,
    _EffortRunResult,
    run_reasoning_effort_benchmark,
)
from cw.codex_runner import CodexRunResult
from tests._codex_review_helpers import _doc_json, _finding_payload

_AUDIT_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex_audit_events"

# Two genuinely distinct captured ``codex exec --json`` streams, so the two
# halves of the comparison are provably not the same numbers twice.
_HEAVY_STREAM = (_AUDIT_FIXTURE_DIR / "clean_with_command.jsonl").read_text(
    encoding="utf-8"
)
_LIGHT_STREAM = (_AUDIT_FIXTURE_DIR / "clean_no_tools.jsonl").read_text(
    encoding="utf-8"
)

_ROLES = ["Code Quality Reviewer", "SysAdmin Reviewer", "Test Reviewer"]
_PROMPTS = {role: f"prompt for {role}" for role in _ROLES}


class _EffortAwareRunner:
    """CodexRunner double whose response depends on the argv's pinned effort.

    Recording ``argv`` per call is what lets the tests assert the harness ran
    each effort value exactly once per role.
    """

    def __init__(
        self,
        *,
        degraded_role: str | None = None,
        degraded_effort: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._degraded_role = degraded_role
        self._degraded_effort = degraded_effort

    @staticmethod
    def pinned_effort(argv: list[str]) -> str | None:
        """Return the ``-c model_reasoning_effort=<value>`` value in *argv*."""
        for index, flag in enumerate(argv):
            if flag == "-c" and index + 1 < len(argv):
                override = argv[index + 1]
                if override.startswith("model_reasoning_effort="):
                    return override.split("=", 1)[1]
        return None

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        del worktree, timeout_seconds
        self.calls.append(list(argv))
        effort = self.pinned_effort(argv)
        role = next((r for r in _ROLES if stdin and r in stdin), _ROLES[0])
        status = "ok"
        if role == self._degraded_role and effort == self._degraded_effort:
            status = "degraded"
        stream = _HEAVY_STREAM if effort == "high" else _LIGHT_STREAM
        findings = [_finding_payload()] if effort == "high" else []
        return CodexRunResult(
            returncode=0,
            stdout=stream,
            stderr="",
            output_file_content=_doc_json(role=role, status=status, findings=findings),
        )


def _run(runner: _EffortAwareRunner, tmp_path: Path) -> BenchmarkComparison:
    return run_reasoning_effort_benchmark(
        runner=runner,
        worktree=tmp_path,
        roles=_ROLES,
        prompts_by_role=_PROMPTS,
        model=None,
        wall_clock_budget_seconds=None,
        session_id="s-benchmark",
    )


class TestRunReasoningEffortBenchmark:
    def test_runs_each_role_once_per_effort_value(self, tmp_path: Path) -> None:
        runner = _EffortAwareRunner()
        comparison = _run(runner, tmp_path)

        # Two passes over three roles: six codex invocations, no more.
        assert len(runner.calls) == 2 * len(_ROLES)
        efforts = [runner.pinned_effort(argv) for argv in runner.calls]
        assert efforts.count("medium") == len(_ROLES)
        assert efforts.count("high") == len(_ROLES)
        assert comparison.medium.effort == "medium"
        assert comparison.high.effort == "high"

    def test_halves_are_distinguishable(self, tmp_path: Path) -> None:
        comparison = _run(_EffortAwareRunner(), tmp_path)

        assert comparison.high.findings_count == len(_ROLES)
        assert comparison.medium.findings_count == 0
        # Distinct captured streams → distinct token totals, per side.
        assert (
            comparison.high.total_input_tokens != comparison.medium.total_input_tokens
        )
        assert (
            comparison.high.total_output_tokens > comparison.medium.total_output_tokens
        )
        assert comparison.high.total_cached_input_tokens == 19968 * len(_ROLES)
        assert comparison.medium.total_cached_input_tokens == 9984 * len(_ROLES)

    def test_degraded_role_lands_on_its_own_effort_only(self, tmp_path: Path) -> None:
        runner = _EffortAwareRunner(
            degraded_role="Test Reviewer", degraded_effort="high"
        )
        comparison = _run(runner, tmp_path)

        assert comparison.high.degraded_roles == ["Test Reviewer"]
        assert comparison.medium.degraded_roles == []

    def test_failed_role_is_recorded_not_raised(self, tmp_path: Path) -> None:
        class _FailingRunner(_EffortAwareRunner):
            def run(
                self,
                worktree: Path,
                argv: list[str],
                timeout_seconds: int | None,
                *,
                stdin: str | None = None,
            ) -> CodexRunResult:
                self.calls.append(list(argv))
                return CodexRunResult(returncode=1, stdout="", stderr="boom")

        comparison = _run(_FailingRunner(), tmp_path)

        assert sorted(comparison.medium.failed_roles) == sorted(_ROLES)
        assert sorted(comparison.high.failed_roles) == sorted(_ROLES)
        assert comparison.medium.findings_count == 0

    def test_custom_effort_values_are_honored(self, tmp_path: Path) -> None:
        runner = _EffortAwareRunner()
        comparison = run_reasoning_effort_benchmark(
            runner=runner,
            worktree=tmp_path,
            roles=_ROLES[:1],
            prompts_by_role=_PROMPTS,
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-benchmark-custom",
            effort_values=("low", "high"),
        )
        assert comparison.medium.effort == "low"
        assert comparison.high.effort == "high"
        assert [runner.pinned_effort(argv) for argv in runner.calls] == ["low", "high"]

    def test_rejects_wrong_number_of_effort_values(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="exactly two"):
            run_reasoning_effort_benchmark(
                runner=_EffortAwareRunner(),
                worktree=tmp_path,
                roles=_ROLES[:1],
                prompts_by_role=_PROMPTS,
                model=None,
                wall_clock_budget_seconds=None,
                session_id="s-benchmark-bad",
                effort_values=("low",),
            )

    def test_result_model_is_serializable(self) -> None:
        result = _EffortRunResult(
            effort="medium",
            findings_count=0,
            degraded_roles=[],
            failed_roles=[],
            wall_clock_seconds=0.0,
            total_input_tokens=0,
            total_cached_input_tokens=0,
            total_output_tokens=0,
            total_reasoning_tokens=0,
        )
        assert result.model_dump(mode="json")["effort"] == "medium"
