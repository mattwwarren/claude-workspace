"""The read-only aggregation turn of a codex engine round (#3216, RFC 0018 A4).

Drives ``cw.codex_review._engine_aggregate.run_codex_aggregator`` against a
:class:`~tests._tool_fakes.ScriptedCodexRunner`. The success cases use the real
codex outputs recorded by the Gate 0 probe (``codex_engine_aggregation_real_*``,
not edited), so the aggregation output model, cw's assembly and the unchanged
summary loader are exercised against what codex actually produced.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path

import pytest

from cw.codex_review._engine_aggregate import run_codex_aggregator
from cw.codex_review._roles import _AUDIT_ARGV_FLAGS
from cw.codex_review._shard import ReviewInputTooLargeError
from cw.codex_runner import CodexRunResult, relabeled
from cw.codex_usage import UsageAccumulator
from cw.models.usage import UsageStage
from cw.review_engine.aggregate import (
    EngineAggregationOutput,
    assemble_summary,
    render_aggregation_envelope,
)
from cw.review_engine.constants import (
    ENGINE_AGGREGATOR_PREAMBLE,
    FRICTION,
    EngineRoundFailure,
)
from cw.review_engine_summary_io import load_review_engine_summary
from tests._codex_review_helpers import _doc_json
from tests._review_engine_helpers import (
    engine_finding,
    make_aggregation_output,
)
from tests._tool_fakes import ScriptedCodexRunner, any_call, codex_usage_stdout

_FIXTURES = Path(__file__).parent / "fixtures"
_REAL_PASS = (_FIXTURES / "codex_engine_aggregation_real_pass.json").read_text(
    encoding="utf-8"
)
_REAL_FAIL = (_FIXTURES / "codex_engine_aggregation_real_fail.json").read_text(
    encoding="utf-8"
)
_HEAD = "ab" * 20
_AGG_PROMPT = "# Aggregation\n\nCombine the role documents.\n"
_ROLE_DOCS = (
    ("security", _doc_json(role="security")),
    ("quality", _doc_json(role="quality")),
)
_AUDIT_REJECTION = "error: unexpected argument '--json' found"


def _ok(content: str | None) -> CodexRunResult:
    return CodexRunResult(0, "", "", output_file_content=content)


def _runner(reply: CodexRunResult) -> ScriptedCodexRunner:
    return ScriptedCodexRunner().add(any_call, reply)


def _run(
    runner: ScriptedCodexRunner,
    summary_path: Path,
    *,
    max_prompt_chars: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: int | None = 90,
) -> object:
    return run_codex_aggregator(
        runner=runner,
        worktree=summary_path.parent,
        head_sha=_HEAD,
        aggregation_prompt=_AGG_PROMPT,
        role_documents=_ROLE_DOCS,
        summary_path=summary_path,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        max_prompt_chars=max_prompt_chars,
    )


def _failure_text(reason: str) -> str:
    return FRICTION[EngineRoundFailure.ENGINE_AGGREGATION_FAILED].format(reason=reason)


class TestArgvAndEnvelope:
    def test_argv_is_read_only_with_schema_output_and_audit_flags(
        self, tmp_path: Path
    ) -> None:
        runner = _runner(_ok(_REAL_PASS))

        _run(runner, tmp_path / "summary.json", model="gpt-x", reasoning_effort="high")

        (call,) = runner.calls
        argv = call["argv"]
        assert isinstance(argv, list)
        assert argv[:2] == ["codex", "exec"]
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "workspace-write" not in argv
        assert "--output-schema" in argv
        assert "-o" in argv
        assert all(flag in argv for flag in _AUDIT_ARGV_FLAGS)
        assert argv[argv.index("-m") + 1] == "gpt-x"
        assert "model_reasoning_effort=high" in argv
        assert call["timeout"] == 90
        assert call["cwd"] == tmp_path

    def test_the_sandbox_is_not_a_parameter_so_a_widened_lane_cannot_reach_it(
        self,
    ) -> None:
        parameters = inspect.signature(run_codex_aggregator).parameters

        assert not [name for name in parameters if "sandbox" in name]

    def test_stdin_is_the_envelope_in_manifest_role_order(self, tmp_path: Path) -> None:
        runner = _runner(_ok(_REAL_PASS))

        _run(runner, tmp_path / "summary.json")

        (call,) = runner.calls
        stdin = call["stdin"]
        assert isinstance(stdin, str)
        assert stdin == render_aggregation_envelope(
            head_sha=_HEAD, aggregation_prompt=_AGG_PROMPT, role_documents=_ROLE_DOCS
        )
        assert stdin.startswith(ENGINE_AGGREGATOR_PREAMBLE)
        assert f"Reviewed head: {_HEAD}" in stdin
        assert _AGG_PROMPT.strip() in stdin
        assert stdin.index("## Role Document: security") < stdin.index(
            "## Role Document: quality"
        )

    def test_the_schema_file_holds_the_strict_aggregation_schema(
        self, tmp_path: Path
    ) -> None:
        seen: dict[str, object] = {}

        def _read_schema(argv: list[str], _stdin: str | None) -> CodexRunResult:
            seen.update(
                json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
            )
            return _ok(_REAL_PASS)

        runner = ScriptedCodexRunner().add(any_call, _read_schema)

        _run(runner, tmp_path / "summary.json")

        golden = json.loads(
            (_FIXTURES / "review_engine_aggregation_schema.json").read_text()
        )
        assert seen == golden

    def test_the_scratch_dir_is_removed_on_success_and_on_failure(
        self, tmp_path: Path
    ) -> None:
        for index, reply in enumerate((_ok(_REAL_PASS), CodexRunResult(2, "", "boom"))):
            runner = _runner(reply)

            _run(runner, tmp_path / f"summary-{index}.json")

            argv = runner.calls[0]["argv"]
            assert isinstance(argv, list)
            scratch = Path(argv[argv.index("-o") + 1]).parent
            assert not scratch.exists()


class TestSuccess:
    @pytest.mark.parametrize(
        ("recorded", "verdict", "findings"),
        [(_REAL_PASS, "PASS", 0), (_REAL_FAIL, "FAIL", 2)],
        ids=["real-pass", "real-fail"],
    )
    def test_the_assembled_summary_lands_exactly_at_the_given_path(
        self, tmp_path: Path, recorded: str, verdict: str, findings: int
    ) -> None:
        summary_path = tmp_path / "round" / "summary.json"
        runner = _runner(_ok(recorded))

        failure = _run(runner, summary_path)

        assert failure is None
        expected = assemble_summary(
            EngineAggregationOutput.model_validate_json(recorded), _HEAD
        )
        assert json.loads(summary_path.read_text(encoding="utf-8")) == expected
        loaded = load_review_engine_summary(summary_path)
        assert loaded.head_sha == _HEAD
        assert loaded.verdict == verdict
        assert len(loaded.findings) == findings
        assert [p.name for p in summary_path.parent.iterdir()] == ["summary.json"]

    def test_a_synthetic_output_built_by_the_helper_is_accepted(
        self, tmp_path: Path
    ) -> None:
        output = make_aggregation_output(
            [engine_finding("MUST_FIX", rule_id="security.injection")]
        )
        summary_path = tmp_path / "summary.json"

        failure = _run(_runner(_ok(json.dumps(output))), summary_path)

        assert failure is None
        assert load_review_engine_summary(summary_path).counts.must_fix == 1


class TestFailures:
    def test_timeout_is_codex_timeout_and_writes_no_summary(
        self, tmp_path: Path
    ) -> None:
        summary_path = tmp_path / "summary.json"
        runner = _runner(CodexRunResult(-1, "", "", timed_out=True))

        failure = _run(runner, summary_path)

        assert failure == (
            EngineRoundFailure.ENGINE_AGGREGATION_FAILED,
            _failure_text("codex_timeout"),
        )
        assert not summary_path.exists()

    def test_a_nonzero_exit_is_codex_error(self, tmp_path: Path) -> None:
        runner = _runner(CodexRunResult(1, "", "boom"))

        failure = _run(runner, tmp_path / "summary.json")

        assert failure == (
            EngineRoundFailure.ENGINE_AGGREGATION_FAILED,
            _failure_text("codex_error"),
        )

    def test_a_missing_runner_binary_is_codex_error(self, tmp_path: Path) -> None:
        runner = _runner(CodexRunResult(127, "", "codex: command not found"))

        failure = _run(runner, tmp_path / "summary.json")

        assert failure == (
            EngineRoundFailure.ENGINE_AGGREGATION_FAILED,
            _failure_text("codex_error"),
        )

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            (None, "missing_output"),
            ("", "empty_output"),
            ("  \n", "empty_output"),
            ("{not json", "invalid_json"),
            ('{"verdict": "PASS"}', "schema_mismatch"),
            (json.dumps(make_aggregation_output(counts={})), "schema_mismatch"),
        ],
        ids=[
            "missing",
            "empty",
            "whitespace",
            "invalid-json",
            "missing-keys",
            "extra-key",
        ],
    )
    def test_an_unusable_exit_zero_output(
        self, tmp_path: Path, content: str | None, reason: str
    ) -> None:
        summary_path = tmp_path / "summary.json"

        failure = _run(_runner(_ok(content)), summary_path)

        assert failure == (
            EngineRoundFailure.ENGINE_AGGREGATION_FAILED,
            _failure_text(reason),
        )
        assert not summary_path.exists()

    def test_deeply_nested_json_is_invalid_json(self, tmp_path: Path) -> None:
        content = "[" * 10_000 + "0" + "]" * 10_000

        failure = _run(_runner(_ok(content)), tmp_path / "summary.json")

        assert failure == (
            EngineRoundFailure.ENGINE_AGGREGATION_FAILED,
            _failure_text("invalid_json"),
        )

    def test_a_summary_write_oserror_is_engine_no_summary(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the round directory must go")
        summary_path = blocker / "summary.json"

        failure = _run(_runner(_ok(_REAL_PASS)), summary_path)

        assert failure == (
            EngineRoundFailure.ENGINE_NO_SUMMARY,
            FRICTION[EngineRoundFailure.ENGINE_NO_SUMMARY].format(
                path=str(summary_path)
            ),
        )


class TestAuditFlagDegrade:
    def test_a_rejection_retries_once_without_the_audit_flags(
        self, tmp_path: Path
    ) -> None:
        summary_path = tmp_path / "summary.json"
        runner = ScriptedCodexRunner()
        runner.add(
            lambda argv, _stdin: "--json" in argv,
            CodexRunResult(2, "", _AUDIT_REJECTION),
        )
        runner.add(any_call, _ok(_REAL_PASS))

        failure = _run(runner, summary_path)

        assert failure is None
        first, second = (call["argv"] for call in runner.calls)
        assert isinstance(first, list)
        assert isinstance(second, list)
        assert all(flag in first for flag in _AUDIT_ARGV_FLAGS)
        assert not any(flag in second for flag in _AUDIT_ARGV_FLAGS)
        assert "-o" in second
        assert runner.calls[0]["stdin"] == runner.calls[1]["stdin"]
        assert summary_path.is_file()

    def test_a_second_rejection_is_not_retried_again(self, tmp_path: Path) -> None:
        runner = _runner(CodexRunResult(2, "", _AUDIT_REJECTION))

        failure = _run(runner, tmp_path / "summary.json")

        assert len(runner.calls) == 2
        assert failure == (
            EngineRoundFailure.ENGINE_AGGREGATION_FAILED,
            _failure_text("codex_error"),
        )

    def test_an_ordinary_nonzero_exit_is_not_retried(self, tmp_path: Path) -> None:
        runner = _runner(CodexRunResult(2, "", "error: model unavailable"))

        _run(runner, tmp_path / "summary.json")

        assert len(runner.calls) == 1


class TestPromptLimit:
    def test_an_envelope_over_the_limit_raises_before_any_codex_call(
        self, tmp_path: Path
    ) -> None:
        envelope = render_aggregation_envelope(
            head_sha=_HEAD, aggregation_prompt=_AGG_PROMPT, role_documents=_ROLE_DOCS
        )
        runner = _runner(_ok(_REAL_PASS))

        with pytest.raises(ReviewInputTooLargeError) as raised:
            _run(runner, tmp_path / "summary.json", max_prompt_chars=len(envelope) - 1)

        assert raised.value.actual_chars == len(envelope)
        assert raised.value.max_chars == len(envelope) - 1
        assert raised.value.file is None
        assert runner.calls == []

    def test_an_envelope_exactly_at_the_limit_runs(self, tmp_path: Path) -> None:
        envelope = render_aggregation_envelope(
            head_sha=_HEAD, aggregation_prompt=_AGG_PROMPT, role_documents=_ROLE_DOCS
        )
        runner = _runner(_ok(_REAL_PASS))

        failure = _run(
            runner, tmp_path / "summary.json", max_prompt_chars=len(envelope)
        )

        assert failure is None
        assert len(runner.calls) == 1


class TestUsageRecording:
    """#3063: the aggregation turn records as ENGINE_AGGREGATE; a retry does not."""

    def test_the_aggregation_turn_is_recorded_under_engine_aggregate(
        self, tmp_path: Path
    ) -> None:
        acc = UsageAccumulator("s-agg", "T-agg", {})
        reply = dataclasses.replace(
            _ok(_REAL_PASS),
            stdout=codex_usage_stdout(input_tokens=500, output_tokens=50),
        )
        runner = relabeled(
            acc.recording_runner(UsageStage.REVIEW_ROLE, _runner(reply)),
            UsageStage.ENGINE_AGGREGATE,
        )

        assert _run(runner, tmp_path / "summary.json") is None

        snapshot = acc.snapshot()
        assert snapshot is not None
        assert list(snapshot.usage.by_stage) == [UsageStage.ENGINE_AGGREGATE]
        assert snapshot.usage.by_stage[UsageStage.ENGINE_AGGREGATE].input_tokens == 500

    def test_the_flag_rejection_retry_records_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        acc = UsageAccumulator("s-agg", "T-agg", {})
        scripted = ScriptedCodexRunner()
        scripted.add(
            lambda argv, _stdin: "--json" in argv,
            CodexRunResult(2, "", _AUDIT_REJECTION),
        )
        scripted.add(any_call, _ok(_REAL_PASS))
        runner = relabeled(
            acc.recording_runner(UsageStage.REVIEW_ROLE, scripted),
            UsageStage.ENGINE_AGGREGATE,
        )

        with caplog.at_level("WARNING", logger="cw.codex_usage"):
            assert _run(runner, tmp_path / "summary.json") is None

        assert acc.snapshot() is None
        assert not caplog.records
