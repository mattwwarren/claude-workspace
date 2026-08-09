"""Tests for cw.codex_review._roles — per-role codex execution, failure
classification, and diagnostics persistence (#1236, #1239, #1330, #1364)."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest

from cw.codex_review import (
    _DISABLED_FEATURES,
    _PROFILE_DIAGNOSTICS_FILENAME,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_TIMEOUT,
    _build_generic_codex_argv,
    _classify_codex_failure,
    _codex_scratch_dir,
    _InstructionSource,
    _is_audit_flag_rejection,
    _run_codex_role,
    run_codex_roles,
)
from cw.codex_runner import CodexRunResult
from cw.config import diagnostics_dir, state_dir
from cw.executor_diagnostics import ExecutorFailure, diagnostics_bundle_dir
from cw.review_findings import (
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    ReviewerRunMetrics,
)
from tests._codex_review_helpers import (
    _Clock,
    _config_override_values,
    _finding_payload,
    _ok_result,
    _SequencedRunner,
)

_AUDIT_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex_audit_events"


def _audit_fixture(name: str) -> str:
    return (_AUDIT_FIXTURE_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _codex_scratch_dir
# ---------------------------------------------------------------------------


class TestCodexScratchDir:
    def test_under_state_dir_not_tmp(self) -> None:
        scratch = _codex_scratch_dir("sess-abc")
        # Must resolve under state_dir() (~/.local/share/cw in production, a
        # snap-readable home path) — never tempfile.TemporaryDirectory()'s /tmp.
        assert scratch.is_relative_to(state_dir())
        assert scratch == state_dir() / "codex-review" / "sess-abc"
        assert scratch.is_dir()


# ---------------------------------------------------------------------------
# _build_generic_codex_argv
# ---------------------------------------------------------------------------


class TestBuildGenericCodexArgv:
    def test_with_model(self, tmp_path: Path) -> None:
        argv = _build_generic_codex_argv(
            model="gpt-5",
            schema_path=tmp_path / "s.json",
            output_path=tmp_path / "o.json",
        )
        assert argv[:2] == ["codex", "exec"]
        assert "review" not in argv
        assert "--base" not in argv
        assert argv[-2:] == ["-m", "gpt-5"]

    def test_no_model(self, tmp_path: Path) -> None:
        argv = _build_generic_codex_argv(
            model=None, schema_path=tmp_path / "s.json", output_path=tmp_path / "o.json"
        )
        assert "-m" not in argv

    def test_read_only_sandbox_always_set(self, tmp_path: Path) -> None:
        # MUST_FIX 4 (#1236): ticket AC requires read-only sandboxing on
        # every generic codex exec invocation, model or no model.
        argv = _build_generic_codex_argv(
            model=None, schema_path=tmp_path / "s.json", output_path=tmp_path / "o.json"
        )
        idx = argv.index("--sandbox")
        assert argv[idx + 1] == "read-only"

    @pytest.mark.parametrize("model", [None, "gpt-5"])
    def test_json_and_ephemeral_always_set(
        self, tmp_path: Path, model: str | None
    ) -> None:
        # #1710: the JSONL audit stream and the no-session-file posture are
        # unconditional, model or no model.
        argv = _build_generic_codex_argv(
            model=model,
            schema_path=tmp_path / "s.json",
            output_path=tmp_path / "o.json",
        )
        assert "--json" in argv
        assert "--ephemeral" in argv
        # The trailing "-m <model>" append contract is unchanged.
        if model is not None:
            assert argv[-2:] == ["-m", model]


# ---------------------------------------------------------------------------
# run_codex_roles — shared deadline (Comment 3)
# ---------------------------------------------------------------------------


class TestRunCodexRoles:
    def test_all_complete_within_budget(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [_ok_result(findings=[_finding_payload()]), _ok_result()]
        )
        docs, failures, metrics_by_role = run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=3600,
            session_id="s-review",
        )
        assert len(docs) == 2
        assert failures == []
        assert len(docs[0].findings) == 1
        assert docs[0].findings[0].severity == "MUST_FIX"
        assert set(metrics_by_role) == {"Code Quality Reviewer", "SysAdmin Reviewer"}

    def test_none_budget_gives_none_timeout(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result(), _ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert all(call["timeout"] is None for call in runner.calls)

    def test_floor_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Test Reviewer SHOULD_FIX 9 (#1236): a real clock + 3600s budget
        # never approaches the floor, so the old version of this test never
        # actually exercised `max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS)`.
        # Deterministic clock: deadline=100 (call0); role1's remaining =
        # 100 - 69 = 31 seconds — just above the 30s skip threshold, close
        # enough to the floor that the clamp is genuinely in play.
        monkeypatch.setattr("cw.codex_review._roles.time.monotonic", _Clock([0, 69]))
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p1"},
            model=None,
            wall_clock_budget_seconds=100,
            session_id="s-review",
        )
        assert runner.calls[0]["timeout"] == 31

    def test_budget_exhausted_skips_later_role(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # deadline=100 (call0). Each successful _run_codex_role now consumes two
        # extra monotonic() reads (start/end duration capture, #1239), so the
        # deadline-remaining reads land at call1 (role1: 100>30 run), call4
        # (role2: 100>30 run), call7 (role3: remaining=100-80=20<=30 -> skip).
        monkeypatch.setattr(
            "cw.codex_review._roles.time.monotonic", _Clock([0, 0, 0, 0, 0, 0, 0, 80])
        )
        runner = _SequencedRunner([_ok_result(), _ok_result()])
        with caplog.at_level(logging.WARNING):
            docs, failures, metrics_by_role = run_codex_roles(
                runner=runner,
                worktree=tmp_path,
                roles=[
                    "Code Quality Reviewer",
                    "SysAdmin Reviewer",
                    "Data Safety Reviewer",
                ],
                prompts_by_role={
                    "Code Quality Reviewer": "p1",
                    "SysAdmin Reviewer": "p2",
                    "Data Safety Reviewer": "p3",
                },
                model=None,
                wall_clock_budget_seconds=100,
                session_id="s-review",
            )
        assert len(docs) == 2
        assert len(failures) == 1
        assert failures[0].role == "Data Safety Reviewer"
        assert failures[0].reason == CODEX_BUDGET_EXHAUSTED
        assert len(runner.calls) == 2  # third role never ran
        # #1710: a budget-exhausted skip never invokes codex, so it contributes
        # no metrics entry; every role that did invoke it gets one.
        assert set(metrics_by_role) == {"Code Quality Reviewer", "SysAdmin Reviewer"}

    def test_per_role_failure_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="boom")]
        )
        with caplog.at_level(logging.WARNING):
            docs, failures, metrics_by_role = run_codex_roles(
                runner=runner,
                worktree=tmp_path,
                roles=["Code Quality Reviewer"],
                prompts_by_role={"Code Quality Reviewer": "p"},
                model=None,
                wall_clock_budget_seconds=None,
                session_id="s-review",
            )
        assert docs == []
        assert failures[0].reason == CODEX_ERROR
        assert any(
            "Code Quality Reviewer" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )
        # #1710: metrics parsing must not crash on a failed role's empty stdout.
        assert set(metrics_by_role) == {"Code Quality Reviewer"}
        assert metrics_by_role["Code Quality Reviewer"]["terminal_event"] is None

    def test_timeout_and_unparseable_failures(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [
                CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True),
                CodexRunResult(
                    returncode=0, stdout="", stderr="", output_file_content="not json"
                ),
            ]
        )
        _docs, failures, metrics_by_role = run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        reasons = {f.role: f.reason for f in failures}
        assert reasons["Code Quality Reviewer"] == CODEX_TIMEOUT
        assert reasons["SysAdmin Reviewer"] == CODEX_REVIEW_UNPARSEABLE
        # #1710: both failure branches still yield a (best-effort, all-default)
        # metrics entry rather than crashing or omitting the role.
        assert set(metrics_by_role) == {"Code Quality Reviewer", "SysAdmin Reviewer"}
        assert all(m["thread_id"] is None for m in metrics_by_role.values())

    def test_native_review_schema_mismatch_one_role_others_succeed(
        self, tmp_path: Path
    ) -> None:
        # MUST_FIX 5 (#1236): synthetic fixture reproducing the historical
        # native-review schema/prose mismatch. Pre-#1236, ``codex exec
        # review`` was fed a schema it sometimes ignored, replying with the
        # OLD ``{must_fix_initial, should_fix, deferred}``-shaped payload (or
        # raw prose) instead of the per-role ReviewerFindingsDocument shape.
        # That payload fails schema validation for the role that produced it
        # (correctly classified as unparseable) but must NOT take down the
        # whole run — the other, well-behaved roles' documents still survive.
        old_shape_payload = json.dumps(
            {"must_fix_initial": 1, "should_fix": 2, "deferred": 0}
        )
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=0,
                    stdout="",
                    stderr="",
                    output_file_content=old_shape_payload,
                ),
                _ok_result(role="SysAdmin Reviewer"),
            ]
        )
        docs, failures, metrics_by_role = run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert len(docs) == 1
        assert docs[0].reviewer_role == "SysAdmin Reviewer"
        assert len(failures) == 1
        assert failures[0].role == "Code Quality Reviewer"
        assert failures[0].reason == CODEX_REVIEW_UNPARSEABLE
        assert set(metrics_by_role) == {"Code Quality Reviewer", "SysAdmin Reviewer"}

    def test_stdin_carries_prompt(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "PROMPT BODY"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert runner.calls[0]["stdin"] == "PROMPT BODY"

    def test_scratch_dir_cleaned_up_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MUST_FIX 1 (#1236): the scratch dir under state_dir() must not leak
        # after a normal, fully-successful run.
        fixed_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")
        monkeypatch.setattr("cw.codex_review._roles.uuid.uuid4", lambda: fixed_uuid)
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert not (state_dir() / "codex-review" / fixed_uuid.hex).exists()

    def test_scratch_dir_cleaned_up_on_role_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cleanup must happen on the failure path too, not only on success.
        fixed_uuid = uuid.UUID("22222222-2222-2222-2222-222222222222")
        monkeypatch.setattr("cw.codex_review._roles.uuid.uuid4", lambda: fixed_uuid)
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="boom")]
        )
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert not (state_dir() / "codex-review" / fixed_uuid.hex).exists()


# ---------------------------------------------------------------------------
# _classify_codex_failure — typed failure taxonomy (#1239)
# ---------------------------------------------------------------------------


class TestClassifyCodexFailure:
    def test_timeout(self) -> None:
        result = CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        assert _classify_codex_failure(result) == "timeout"

    def test_spawn_error_command_not_found(self) -> None:
        result = CodexRunResult(
            returncode=127, stdout="", stderr="codex: command not found"
        )
        assert _classify_codex_failure(result) == "spawn_error"

    def test_nonzero_exit(self) -> None:
        result = CodexRunResult(returncode=1, stdout="", stderr="boom")
        assert _classify_codex_failure(result) == "nonzero_exit"

    def test_missing_output(self) -> None:
        result = CodexRunResult(
            returncode=0, stdout="", stderr="", output_file_content=None
        )
        assert _classify_codex_failure(result) == "missing_output"

    def test_empty_output(self) -> None:
        result = CodexRunResult(
            returncode=0, stdout="", stderr="", output_file_content="   "
        )
        assert _classify_codex_failure(result) == "empty_output"

    def test_invalid_json(self) -> None:
        result = CodexRunResult(
            returncode=0, stdout="", stderr="", output_file_content="{not json"
        )
        assert _classify_codex_failure(result) == "invalid_json"

    def test_schema_mismatch(self) -> None:
        result = CodexRunResult(
            returncode=0,
            stdout="",
            stderr="",
            output_file_content='{"unexpected": "shape"}',
        )
        assert _classify_codex_failure(result) == "schema_mismatch"


def _bundle_file(session_id: str, role_slug: str, category: str) -> Path:
    """Return the single bundle JSON matching *role_slug*/*category* (#1330).

    Filenames now carry an ``occurred_at`` timestamp suffix (item 7), so exact
    filenames are no longer stable across a test run — glob on the stable
    prefix instead.
    """
    bundle = diagnostics_bundle_dir(session_id)
    matches = [
        p
        for p in bundle.glob(f"{role_slug}-{category}-*.json")
        if not p.name.endswith(("-schema.json", "-output.json"))
    ]
    assert len(matches) == 1, (
        f"expected exactly one {role_slug}-{category}-*.json bundle file, "
        f"found {matches}"
    )
    return matches[0]


def _run_one_role(
    runner: _SequencedRunner,
    tmp_path: Path,
    *,
    session_id: str = "sess-diag",
    role: str = "Code Quality Reviewer",
) -> tuple[
    ReviewerFindingsDocument | None, ReviewerRunFailure | None, ReviewerRunMetrics
]:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return _run_codex_role(
        runner=runner,
        worktree=tmp_path,
        role=role,
        prompt="p",
        model=None,
        timeout_seconds=None,
        scratch_dir=scratch,
        session_id=session_id,
    )


def test_run_codex_role_spawn_error_surfaces_codex_error_reason(
    tmp_path: Path,
) -> None:
    """A spawn_error-shaped CodexRunResult (codex binary missing) surfaces as
    ReviewerRunFailure.reason == CODEX_ERROR through _CATEGORY_TO_REASON, while
    the persisted bundle's category stays the fine-grained 'spawn_error'."""
    runner = _SequencedRunner(
        [CodexRunResult(returncode=127, stdout="", stderr="codex: command not found")]
    )
    _doc, failure, _metrics = _run_one_role(runner, tmp_path, session_id="sess-spawn")
    assert failure is not None
    assert failure.reason == CODEX_ERROR
    path = _bundle_file("sess-spawn", "code-quality-reviewer", "spawn_error")
    persisted = ExecutorFailure.model_validate_json(path.read_text())
    assert persisted.category == "spawn_error"


# ---------------------------------------------------------------------------
# _run_codex_role — diagnostics persistence on failure (#1239)
# ---------------------------------------------------------------------------


class TestRunCodexRolePersistsDiagnostics:
    def test_persists_diagnostics_on_timeout(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)]
        )
        _run_one_role(runner, tmp_path)
        path = _bundle_file("sess-diag", "code-quality-reviewer", "timeout")
        assert path.exists()
        failure = ExecutorFailure.model_validate_json(path.read_text())
        assert failure.category == "timeout"
        assert failure.executor_name == "codex"
        assert failure.reviewer_role == "Code Quality Reviewer"
        assert failure.session_id == "sess-diag"

    def test_persists_diagnostics_on_nonzero_exit(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="boom")]
        )
        _run_one_role(runner, tmp_path)
        path = _bundle_file("sess-diag", "code-quality-reviewer", "nonzero_exit")
        assert path.exists()
        assert (
            ExecutorFailure.model_validate_json(path.read_text()).category
            == "nonzero_exit"
        )

    @pytest.mark.parametrize(
        ("output_content", "category"),
        [
            (None, "missing_output"),
            ("", "empty_output"),
            ("{not json", "invalid_json"),
            ('{"unexpected": "shape"}', "schema_mismatch"),
        ],
    )
    def test_persists_diagnostics_on_unparseable_output_variants(
        self, tmp_path: Path, output_content: str | None, category: str
    ) -> None:
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=0,
                    stdout="",
                    stderr="",
                    output_file_content=output_content,
                )
            ]
        )
        _run_one_role(runner, tmp_path)
        path = _bundle_file("sess-diag", "code-quality-reviewer", category)
        assert path.exists()
        assert (
            ExecutorFailure.model_validate_json(path.read_text()).category == category
        )

    def test_success_does_not_persist_diagnostics(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        doc, failure, _metrics = _run_one_role(runner, tmp_path)
        assert doc is not None
        assert failure is None
        assert not diagnostics_bundle_dir("sess-diag").exists()

    def test_secret_shaped_stderr_is_redacted_in_persisted_bundle(
        self, tmp_path: Path
    ) -> None:
        # Drives a secret-shaped string through the real production path
        # (_run_codex_role -> _persist_codex_role_diagnostics) rather than
        # unit-testing redact() in isolation, so a future call site that
        # forgets to route stderr through the ExecutorFailure validator would
        # be caught here.
        secret = "sk-" + "a" * 40
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr=f"boom: {secret}")]
        )
        _run_one_role(runner, tmp_path)
        path = _bundle_file("sess-diag", "code-quality-reviewer", "nonzero_exit")
        failure = ExecutorFailure.model_validate_json(path.read_text())
        assert secret not in failure.stderr_excerpt
        assert "<redacted>" in failure.stderr_excerpt


# ---------------------------------------------------------------------------
# _run_codex_role — writes an OpenAI strict-mode schema file (#1364)
# ---------------------------------------------------------------------------


class TestRunCodexRoleWritesStrictSchema:
    def test_schema_file_content_is_strict(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        _run_codex_role(
            runner=runner,
            worktree=tmp_path,
            role="Code Quality Reviewer",
            prompt="p",
            model=None,
            timeout_seconds=None,
            scratch_dir=scratch,
            session_id="sess-strict",
        )
        schema_path = scratch / "code-quality-reviewer-schema.json"
        schema = json.loads(schema_path.read_text())

        assert schema["additionalProperties"] is False
        assert schema["$defs"]["Finding"]["additionalProperties"] is False
        assert schema["$defs"]["EscalationMetadata"]["additionalProperties"] is False

        nodes = [
            schema,
            schema["$defs"]["Finding"],
            schema["$defs"]["EscalationMetadata"],
        ]
        for node in nodes:
            assert set(node["required"]) == set(node["properties"].keys())


def test_run_codex_roles_scratch_dir_still_removed_after_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even when a role failure triggers a diagnostics copy from the scratch dir,
    # run_codex_roles still removes the scratch dir before returning; the
    # persisted bundle (under a different tree) survives.
    fixed_uuid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    monkeypatch.setattr("cw.codex_review._roles.uuid.uuid4", lambda: fixed_uuid)
    runner = _SequencedRunner([CodexRunResult(returncode=1, stdout="", stderr="boom")])
    run_codex_roles(
        runner=runner,
        worktree=tmp_path,
        roles=["Code Quality Reviewer"],
        prompts_by_role={"Code Quality Reviewer": "p"},
        model=None,
        wall_clock_budget_seconds=None,
        session_id="sess-scratch",
    )
    assert not (state_dir() / "codex-review" / fixed_uuid.hex).exists()
    assert _bundle_file(
        "sess-scratch", "code-quality-reviewer", "nonzero_exit"
    ).exists()


def test_duration_captured_without_extending_codex_run_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CodexRunResult stays free of a duration attribute; the persisted
    # ExecutorFailure.duration_seconds is populated from a monotonic() delta.
    monkeypatch.setattr("cw.codex_review._roles.time.monotonic", _Clock([100.0, 105.5]))
    runner = _SequencedRunner([CodexRunResult(returncode=1, stdout="", stderr="boom")])
    _run_one_role(runner, tmp_path, session_id="sess-dur")
    result = runner._results[0]
    assert not hasattr(result, "duration")
    path = _bundle_file("sess-dur", "code-quality-reviewer", "nonzero_exit")
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.duration_seconds == pytest.approx(5.5)


# ---------------------------------------------------------------------------
# _is_audit_flag_rejection — the narrow degrade-and-retry gate (#1710)
# ---------------------------------------------------------------------------


class TestIsAuditFlagRejection:
    @pytest.mark.parametrize(
        ("returncode", "timed_out", "stderr", "expected"),
        [
            # clap-style rejection naming --json
            (2, False, "error: unexpected argument '--json' found", True),
            # ...and naming --ephemeral
            (2, False, "error: unexpected argument '--ephemeral' found", True),
            # other marker phrasings still match
            (1, False, "unrecognized option '--json'", True),
            (1, False, "unknown argument: --ephemeral", True),
            # a clean exit is never a flag rejection, whatever stderr says
            (0, False, "error: unexpected argument '--json' found", False),
            # a timeout is never a flag rejection
            (-1, True, "error: unexpected argument '--json' found", False),
            # marker present but neither flag named -> not our rejection
            (2, False, "error: unrecognized argument '--wibble' found", False),
            # flag named but no rejection marker -> an ordinary failure that
            # happens to echo the argv back
            (1, False, "codex exec --json failed: upstream 500", False),
            (1, False, "", False),
        ],
    )
    def test_table(
        self, returncode: int, timed_out: bool, stderr: str, expected: bool
    ) -> None:
        result = CodexRunResult(
            returncode=returncode, stdout="", stderr=stderr, timed_out=timed_out
        )
        assert _is_audit_flag_rejection(result) is expected

    def test_marker_match_is_case_insensitive(self) -> None:
        result = CodexRunResult(
            returncode=2, stdout="", stderr="ERROR: Unexpected Argument '--json'"
        )
        assert _is_audit_flag_rejection(result) is True


# ---------------------------------------------------------------------------
# _run_codex_role — audit metrics + flag-rejection degrade-and-retry (#1710)
# ---------------------------------------------------------------------------


class TestRunCodexRoleAuditMetrics:
    def test_success_populates_metrics_from_jsonl_stdout(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [_ok_result(stdout=_audit_fixture("clean_with_command.jsonl"))]
        )
        doc, failure, metrics = _run_one_role(runner, tmp_path, session_id="sess-m1")
        assert doc is not None
        assert failure is None
        assert metrics["thread_id"] == "<THREAD_ID>"
        assert metrics["terminal_event"] == "turn.completed"
        assert metrics["input_tokens"] == 26617
        assert metrics["tool_call_counts"]["command_execution"] == 1
        assert metrics["had_command_evidence"] is True

    def test_success_duration_populated_without_a_third_clock_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Success-path counterpart to
        # test_duration_captured_without_extending_codex_run_result: exactly two
        # monotonic() reads per _run_codex_role, same as before #1710.
        clock = _Clock([100.0, 107.25])
        monkeypatch.setattr("cw.codex_review._roles.time.monotonic", clock)
        runner = _SequencedRunner(
            [_ok_result(stdout=_audit_fixture("clean_no_tools.jsonl"))]
        )
        _doc, _failure, metrics = _run_one_role(runner, tmp_path, session_id="sess-m2")
        assert metrics["duration_seconds"] == pytest.approx(7.25)

    def test_failure_branch_still_returns_metrics(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=1,
                    stdout=_audit_fixture("failed_turn.jsonl"),
                    stderr="boom",
                )
            ]
        )
        doc, failure, metrics = _run_one_role(runner, tmp_path, session_id="sess-m3")
        assert doc is None
        assert failure is not None
        assert metrics["terminal_event"] == "turn.failed"
        assert metrics["thread_id"] == "<THREAD_ID>"

    def test_malformed_terminal_event_warns_with_role_and_session(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A first-call success whose JSONL was cut off: the warning lives in
        # _run_codex_role (it has role/session_id to attribute it), not in the
        # parser.
        runner = _SequencedRunner(
            [_ok_result(stdout=_audit_fixture("truncated_mid_command.jsonl"))]
        )
        with caplog.at_level(logging.WARNING):
            doc, failure, metrics = _run_one_role(
                runner, tmp_path, session_id="sess-truncated"
            )
        assert doc is not None
        assert failure is None
        assert metrics["terminal_event"] == "item.completed"
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "Code Quality Reviewer" in msg and "sess-truncated" in msg
            for msg in warnings
        ), warnings

    def test_healthy_terminal_event_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = _SequencedRunner(
            [_ok_result(stdout=_audit_fixture("clean_no_tools.jsonl"))]
        )
        with caplog.at_level(logging.WARNING):
            _run_one_role(runner, tmp_path, session_id="sess-clean")
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestRunCodexRoleFlagRejectionRetry:
    def test_retry_without_audit_flags_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock([100.0, 111.5])
        monkeypatch.setattr("cw.codex_review._roles.time.monotonic", clock)
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=2,
                    stdout="",
                    stderr="error: unexpected argument '--json' found",
                    output_file_content=None,
                ),
                _ok_result(),
            ]
        )
        doc, failure, metrics = _run_one_role(runner, tmp_path, session_id="sess-retry")
        # (a) the role ultimately succeeded — a real document, no failure
        assert doc is not None
        assert failure is None
        # (b) exactly one retry, with the audit flags stripped
        assert len(runner.calls) == 2
        first_argv = runner.calls[0]["argv"]
        second_argv = runner.calls[1]["argv"]
        assert isinstance(first_argv, list)
        assert isinstance(second_argv, list)
        assert "--json" in first_argv
        assert "--ephemeral" in first_argv
        assert "--json" not in second_argv
        assert "--ephemeral" not in second_argv
        # the rest of the argv contract survives the strip
        assert second_argv[:2] == ["codex", "exec"]
        assert "--output-schema" in second_argv
        assert "-o" in second_argv
        # (c) no JSONL was produced on the retry -> all-default metrics
        assert metrics["thread_id"] is None
        assert metrics["terminal_event"] is None
        assert metrics["tool_call_counts"] == {}
        # (d) duration still comes from the single start/end monotonic() pair,
        # i.e. total wall time across BOTH invocations
        assert metrics["duration_seconds"] == pytest.approx(11.5)
        # (e) the role succeeded, so no diagnostics bundle was written
        assert not diagnostics_bundle_dir("sess-retry").exists()

    def test_retry_success_does_not_warn_about_missing_terminal_event(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The retry's metrics["terminal_event"] is None too, but the argv no
        # longer carries --json, so the malformed-stream warning must not fire.
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=2,
                    stdout="",
                    stderr="error: unexpected argument '--ephemeral' found",
                ),
                _ok_result(),
            ]
        )
        with caplog.at_level(logging.WARNING):
            doc, _failure, metrics = _run_one_role(
                runner, tmp_path, session_id="sess-retry-quiet"
            )
        assert doc is not None
        assert metrics["terminal_event"] is None
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_unrelated_nonzero_exit_is_not_retried(self, tmp_path: Path) -> None:
        # Narrow flag-capability degrade, NOT a general retry-on-any-failure.
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="some other codex error")]
        )
        doc, failure, _metrics = _run_one_role(
            runner, tmp_path, session_id="sess-noretry"
        )
        assert len(runner.calls) == 1
        assert doc is None
        assert failure is not None
        assert failure.reason == CODEX_ERROR
        assert _bundle_file(
            "sess-noretry", "code-quality-reviewer", "nonzero_exit"
        ).exists()

    def test_timeout_is_not_retried(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)]
        )
        _doc, failure, _metrics = _run_one_role(
            runner, tmp_path, session_id="sess-noretry-timeout"
        )
        assert len(runner.calls) == 1
        assert failure is not None
        assert failure.reason == CODEX_TIMEOUT

    def test_spawn_error_is_not_retried(self, tmp_path: Path) -> None:
        # _classify_codex_failure returns "spawn_error", not "nonzero_exit",
        # so the retry gate never opens even though stderr could match.
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=127,
                    stdout="",
                    stderr="codex: command not found",
                )
            ]
        )
        _doc, failure, _metrics = _run_one_role(
            runner, tmp_path, session_id="sess-noretry-spawn"
        )
        assert len(runner.calls) == 1
        assert failure is not None
        assert _bundle_file(
            "sess-noretry-spawn", "code-quality-reviewer", "spawn_error"
        ).exists()

    def test_failed_retry_falls_through_to_the_normal_failure_path(
        self, tmp_path: Path
    ) -> None:
        # The retry's result becomes THE result: classification and the
        # persisted argv both reflect the second invocation.
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=2,
                    stdout="",
                    stderr="error: unexpected argument '--json' found",
                ),
                CodexRunResult(returncode=1, stdout="", stderr="still broken"),
            ]
        )
        doc, failure, _metrics = _run_one_role(
            runner, tmp_path, session_id="sess-retry-fail"
        )
        assert len(runner.calls) == 2
        assert doc is None
        assert failure is not None
        assert failure.reason == CODEX_ERROR
        path = _bundle_file("sess-retry-fail", "code-quality-reviewer", "nonzero_exit")
        persisted = ExecutorFailure.model_validate_json(path.read_text())
        # argv was reassigned in place, so diagnostics name what actually ran.
        assert "--json" not in persisted.argv_sanitized
        assert "--ephemeral" not in persisted.argv_sanitized
        assert "still broken" in persisted.stderr_excerpt


# ---------------------------------------------------------------------------
# Lean reviewer profile threading (#1711)
# ---------------------------------------------------------------------------


class TestBuildGenericCodexArgvLeanProfile:
    @pytest.mark.parametrize("model", [None, "gpt-5"])
    def test_lean_profile_flags_present(
        self, tmp_path: Path, model: str | None
    ) -> None:
        argv = _build_generic_codex_argv(
            model=model,
            schema_path=tmp_path / "s.json",
            output_path=tmp_path / "o.json",
            reasoning_effort="high",
        )
        assert "--ignore-user-config" in argv
        assert "--ignore-rules" in argv
        assert "--strict-config" in argv
        overrides = _config_override_values(argv)
        assert "project_doc_max_bytes=0" in overrides
        assert "mcp_servers={}" in overrides
        assert "model_reasoning_effort=high" in overrides
        for feature in _DISABLED_FEATURES:
            assert argv[argv.index(feature) - 1] == "--disable"
        # The trailing "-m <model>" append contract is unchanged.
        if model is not None:
            assert argv[-2:] == ["-m", model]

    def test_reasoning_effort_defaults_to_omitted(self, tmp_path: Path) -> None:
        # Mirrors test_no_model: the builder-level default is "do not pin it";
        # the "high" default lives on StageExecutorConfig, not here.
        argv = _build_generic_codex_argv(
            model=None,
            schema_path=tmp_path / "s.json",
            output_path=tmp_path / "o.json",
        )
        assert not any(
            o.startswith("model_reasoning_effort=")
            for o in _config_override_values(argv)
        )

    def test_lean_block_sits_after_the_sandbox_pair(self, tmp_path: Path) -> None:
        argv = _build_generic_codex_argv(
            model=None,
            schema_path=tmp_path / "s.json",
            output_path=tmp_path / "o.json",
            reasoning_effort="medium",
        )
        assert argv[:4] == ["codex", "exec", "--sandbox", "read-only"]
        assert argv.index("--ignore-user-config") > argv.index("read-only")
        assert argv.index("--ignore-user-config") < argv.index("--json")


class TestRunCodexRoleEffectiveModel:
    @pytest.mark.parametrize("model", ["gpt-5-codex", None])
    def test_metrics_records_the_resolved_model(
        self, tmp_path: Path, model: str | None
    ) -> None:
        runner = _SequencedRunner([_ok_result()])
        _doc, failure, metrics = _run_codex_role(
            runner=runner,
            worktree=tmp_path,
            role="Code Quality Reviewer",
            prompt="p",
            model=model,
            timeout_seconds=None,
            scratch_dir=tmp_path,
            session_id="s-effective-model",
        )
        assert failure is None
        assert metrics["effective_model"] == model

    def test_effective_model_recorded_on_the_failure_branch(
        self, tmp_path: Path
    ) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="boom")]
        )
        _doc, failure, metrics = _run_codex_role(
            runner=runner,
            worktree=tmp_path,
            role="Code Quality Reviewer",
            prompt="p",
            model="gpt-5-codex",
            timeout_seconds=None,
            scratch_dir=tmp_path,
            session_id="s-effective-model-fail",
        )
        assert failure is not None
        assert metrics["effective_model"] == "gpt-5-codex"


class TestRunCodexRolesProfileThreading:
    def test_reasoning_effort_reaches_every_role_argv(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result(), _ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p1", "SysAdmin Reviewer": "p2"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-effort",
            reasoning_effort="medium",
        )
        assert len(runner.calls) == 2
        for call in runner.calls:
            argv = call["argv"]
            assert isinstance(argv, list)
            assert "model_reasoning_effort=medium" in _config_override_values(argv)

    def test_profile_diagnostics_written_once_per_invocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, object]] = []

        def _spy(
            *,
            session_id: str,
            model: str | None,
            reasoning_effort: str | None,
            instruction_sources: list[_InstructionSource],
        ) -> None:
            seen.append(
                {
                    "session_id": session_id,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "instruction_sources": instruction_sources,
                }
            )

        monkeypatch.setattr("cw.codex_review._roles._persist_profile_diagnostics", _spy)
        runner = _SequencedRunner([_ok_result(), _ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p1", "SysAdmin Reviewer": "p2"},
            model="gpt-5-codex",
            wall_clock_budget_seconds=None,
            session_id="s-profile-diag",
            reasoning_effort="high",
            instruction_sources=[
                _InstructionSource.ROLE_SPEC,
                _InstructionSource.APPROVED_PLAN,
            ],
        )
        # Once per invocation — not once per role.
        assert seen == [
            {
                "session_id": "s-profile-diag",
                "model": "gpt-5-codex",
                "reasoning_effort": "high",
                "instruction_sources": ["role_spec", "approved_plan"],
            }
        ]

    def test_profile_diagnostics_artifact_lands_on_disk(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p1"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-profile-artifact",
            reasoning_effort="high",
            instruction_sources=[_InstructionSource.TICKET_CONTEXT],
        )
        path = diagnostics_dir("s-profile-artifact") / _PROFILE_DIAGNOSTICS_FILENAME
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["reasoning_effort"] == "high"
        assert data["instruction_sources"] == ["ticket_context"]

    def test_defaults_omit_effort_and_sources(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p1"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-profile-defaults",
        )
        argv = runner.calls[0]["argv"]
        assert isinstance(argv, list)
        assert not any(
            o.startswith("model_reasoning_effort=")
            for o in _config_override_values(argv)
        )
        path = diagnostics_dir("s-profile-defaults") / _PROFILE_DIAGNOSTICS_FILENAME
        assert json.loads(path.read_text(encoding="utf-8"))["instruction_sources"] == []
