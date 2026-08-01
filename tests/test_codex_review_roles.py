"""Tests for cw.codex_review._roles — per-role codex execution, failure
classification, and diagnostics persistence (#1236, #1239, #1330, #1364)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

import pytest

from cw.codex_review import (
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_TIMEOUT,
    _build_generic_codex_argv,
    _classify_codex_failure,
    _codex_scratch_dir,
    _run_codex_role,
    run_codex_roles,
)
from cw.codex_runner import CodexRunResult
from cw.config import state_dir
from cw.executor_diagnostics import ExecutorFailure, diagnostics_bundle_dir
from tests._codex_review_helpers import _Clock, _finding_json, _ok_result, _SequencedRunner

if TYPE_CHECKING:
    from pathlib import Path


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


# ---------------------------------------------------------------------------
# run_codex_roles — shared deadline (Comment 3)
# ---------------------------------------------------------------------------


class TestRunCodexRoles:
    def test_all_complete_within_budget(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [_ok_result(findings=[_finding_json()]), _ok_result()]
        )
        docs, failures = run_codex_roles(
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
            docs, failures = run_codex_roles(
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

    def test_per_role_failure_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="boom")]
        )
        with caplog.at_level(logging.WARNING):
            docs, failures = run_codex_roles(
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

    def test_timeout_and_unparseable_failures(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [
                CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True),
                CodexRunResult(
                    returncode=0, stdout="", stderr="", output_file_content="not json"
                ),
            ]
        )
        _docs, failures = run_codex_roles(
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
        docs, failures = run_codex_roles(
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
) -> tuple[object, object]:
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
    _doc, failure = _run_one_role(runner, tmp_path, session_id="sess-spawn")
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
        doc, failure = _run_one_role(runner, tmp_path)
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
