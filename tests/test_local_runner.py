"""Tests for cw.local_runner — AiderRunner + git synthesis (RFC 0005 F3)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from cw.auto_dev_result import AutoDevResult
from cw.local_runner import (
    _FIXED_HEALTH,
    AIDER_ERROR,
    AIDER_NO_OUTPUT,
    AIDER_NOT_FOUND,
    BUDGET_EXCEEDED,
    PLAN_MISSING,
    AiderRunResult,
    FakeAiderRunner,
    RealAiderRunner,
    _blocked_scope,
    build_argv,
    build_env,
    build_task_message,
    synthesize_result,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Fixtures: inherit tmp_config_dir autouse (redirects state paths).
# make_git_repo creates a real git repo for synthesize_result tests.
# ---------------------------------------------------------------------------


def _make_task(ticket_id: str = "T-1", scope_hint: str | None = None) -> MagicMock:
    task = MagicMock()
    task.ticket_id = ticket_id
    task.scope_hint = scope_hint
    return task


# ---------------------------------------------------------------------------
# FakeAiderRunner — records argv/env/cwd/timeout
# ---------------------------------------------------------------------------


def test_aider_runner_records_argv(tmp_path: Path) -> None:
    """FakeAiderRunner.run() records argv, cwd, env, and timeout per call."""
    runner = FakeAiderRunner(returncode=0, stdout="", stderr="")
    argv = ["aider", "--model", "openai/test", "--message", "do stuff"]
    env = {"OPENAI_API_BASE": "http://localhost:1234/v1", "OPENAI_API_KEY": "local"}

    runner.run(tmp_path, argv, env, 900)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"] == argv
    assert call["cwd"] == tmp_path
    assert call["env"] == env
    assert call["timeout"] == 900


def test_fake_runner_returns_configured_result(tmp_path: Path) -> None:
    """FakeAiderRunner returns the configured returncode/stdout/stderr."""
    runner = FakeAiderRunner(returncode=1, stderr="some error")
    result = runner.run(tmp_path, ["aider"], {}, None)
    assert result.returncode == 1
    assert result.stderr == "some error"
    assert result.timed_out is False


def test_fake_runner_raise_timeout_flag(tmp_path: Path) -> None:
    """FakeAiderRunner with raise_timeout=True returns timed_out=True."""
    runner = FakeAiderRunner(raise_timeout=True)
    result = runner.run(tmp_path, ["aider"], {}, 60)
    assert result.timed_out is True
    assert result.returncode == -1


# ---------------------------------------------------------------------------
# RealAiderRunner — subprocess handling
# ---------------------------------------------------------------------------


def test_run_aider_not_found(tmp_path: Path) -> None:
    """RealAiderRunner.run() catches FileNotFoundError when binary is absent."""
    runner = RealAiderRunner()
    result = runner.run(tmp_path, ["aider-nonexistent-binary-xyz"], {}, None)
    assert not result.timed_out
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_run_aider_timeout(tmp_path: Path) -> None:
    """RealAiderRunner.run() catches TimeoutExpired and sets timed_out=True."""
    runner = RealAiderRunner()
    # "sleep 60" will be killed by a 0-second timeout.
    result = runner.run(tmp_path, ["sleep", "60"], {}, 0)
    assert result.timed_out is True


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


def test_build_argv_prepends_openai_prefix() -> None:
    """build_argv prepends 'openai/' to a model without it."""
    argv = build_argv("qwen2.5-coder-32b-instruct", "task")
    assert "--model" in argv
    model = argv[argv.index("--model") + 1]
    assert model == "openai/qwen2.5-coder-32b-instruct"


def test_build_argv_no_double_prefix() -> None:
    """build_argv does not double-prepend 'openai/'."""
    argv = build_argv("openai/qwen2.5-coder-32b-instruct", "task")
    model = argv[argv.index("--model") + 1]
    assert model == "openai/qwen2.5-coder-32b-instruct"
    assert model.count("openai/") == 1


def test_build_argv_includes_required_flags() -> None:
    """build_argv includes all required aider flags."""
    argv = build_argv("model", "my task")
    assert "--yes" in argv
    assert "--auto-commits" in argv
    assert "--no-pretty" in argv
    assert "--no-browser" in argv
    assert "--no-auto-lint" in argv
    assert "--no-auto-test" in argv
    assert "--no-stream" in argv
    assert "--map-tokens" in argv


# ---------------------------------------------------------------------------
# build_env
# ---------------------------------------------------------------------------


def test_build_env_sets_api_base() -> None:
    """build_env sets OPENAI_API_BASE to the given endpoint."""
    env = build_env("http://localhost:1234/v1")
    assert env["OPENAI_API_BASE"] == "http://localhost:1234/v1"


def test_build_env_sets_api_key_fallback() -> None:
    """build_env sets OPENAI_API_KEY to 'local' when env var is absent."""
    with patch.dict(os.environ, {}, clear=False):
        env_without_key = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            env = build_env("http://localhost:1234/v1")
    assert env.get("OPENAI_API_KEY") == "local"


# ---------------------------------------------------------------------------
# build_task_message
# ---------------------------------------------------------------------------


def test_build_task_message_missing_plan_returns_none(tmp_path: Path) -> None:
    """build_task_message returns None when .cw/plan.md is absent."""
    result = build_task_message(tmp_path)
    assert result is None


def test_build_task_message_reads_plan(tmp_path: Path) -> None:
    """build_task_message reads .cw/plan.md when present."""
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text("Do the thing.\n", encoding="utf-8")

    result = build_task_message(tmp_path)

    assert result is not None
    assert "Do the thing." in result


def test_build_task_message_supplements_context(tmp_path: Path) -> None:
    """build_task_message adds ticket title/body from .cw/context.json when present."""
    import json

    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text("plan content", encoding="utf-8")
    (cw_dir / "context.json").write_text(
        json.dumps({"title": "My Ticket", "body": "Ticket body"}),
        encoding="utf-8",
    )

    result = build_task_message(tmp_path)

    assert result is not None
    assert "My Ticket" in result
    assert "Ticket body" in result
    assert "plan content" in result


# ---------------------------------------------------------------------------
# synthesize_result — all disposition paths
# ---------------------------------------------------------------------------


def _write_plan(worktree: Path) -> None:
    """Helper: write a minimal .cw/plan.md so build_task_message succeeds."""
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")


def _add_commit(worktree: Path, message: str = "aider commit") -> None:
    """Add a new commit to the worktree (simulates aider output)."""
    test_file = worktree / "aider_output.py"
    test_file.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", message],
        check=True,
        capture_output=True,
    )


def test_synthesize_result_stage_complete(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """aider exit 0 with ≥1 new commit → stage_complete."""
    worktree = make_git_repo("wt-complete")
    # Simulate a remote 'origin/main' ref by creating it locally.
    subprocess.run(
        ["git", "-C", str(worktree), "remote", "add", "origin", str(worktree)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "fetch", "origin", "main"],
        check=True,
        capture_output=True,
    )
    _add_commit(worktree)

    run_result = AiderRunResult(returncode=0, stdout="", stderr="")
    task = _make_task(ticket_id="T-1", scope_hint="small")

    result = synthesize_result(
        task=task,
        worktree=worktree,
        run_result=run_result,
        default_branch="main",
    )

    assert result.status == "stage_complete"
    assert result.stage_reached == "stage2_impl"
    assert len(result.commits) >= 1
    assert result.scope.lines_actual is not None
    assert result.scope.tier == "small"
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_synthesize_result_blocked_aider_no_output(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """aider exit 0 with no new commits → blocked/aider_no_output."""
    worktree = make_git_repo("wt-no-output")
    subprocess.run(
        ["git", "-C", str(worktree), "remote", "add", "origin", str(worktree)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "fetch", "origin", "main"],
        check=True,
        capture_output=True,
    )
    run_result = AiderRunResult(returncode=0, stdout="", stderr="")
    task = _make_task()

    result = synthesize_result(
        task=task,
        worktree=worktree,
        run_result=run_result,
        default_branch="main",
    )

    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == AIDER_NO_OUTPUT
    assert result.blocker.retry_eligible is True
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_synthesize_result_blocked_aider_error(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """aider exit non-zero → blocked/aider_error with stderr tail in details."""
    worktree = make_git_repo("wt-error")
    run_result = AiderRunResult(returncode=1, stdout="", stderr="fatal: some error")
    task = _make_task()

    result = synthesize_result(
        task=task,
        worktree=worktree,
        run_result=run_result,
        default_branch="main",
    )

    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == AIDER_ERROR
    assert "some error" in result.blocker.details
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_synthesize_result_blocked_budget_exceeded(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """timed_out=True → blocked/budget_exceeded with retry_eligible=True."""
    worktree = make_git_repo("wt-timeout")
    run_result = AiderRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
    task = _make_task()

    result = synthesize_result(
        task=task,
        worktree=worktree,
        run_result=run_result,
        default_branch="main",
    )

    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == BUDGET_EXCEEDED
    assert result.blocker.retry_eligible is True
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_synthesize_result_scope_tier_large(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """task.scope_hint='large' → scope.tier='large' in stage_complete result."""
    worktree = make_git_repo("wt-large")
    subprocess.run(
        ["git", "-C", str(worktree), "remote", "add", "origin", str(worktree)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "fetch", "origin", "main"],
        check=True,
        capture_output=True,
    )
    _add_commit(worktree)

    task = _make_task(scope_hint="large")
    run_result = AiderRunResult(returncode=0, stdout="", stderr="")

    result = synthesize_result(
        task=task,
        worktree=worktree,
        run_result=run_result,
        default_branch="main",
    )

    assert result.status == "stage_complete"
    assert result.scope.tier == "large"


# ---------------------------------------------------------------------------
# Early-exit constants (plan spec: assert exact values)
# ---------------------------------------------------------------------------


def test_early_exit_scope_health_constants() -> None:
    """_blocked_scope and _FIXED_HEALTH values satisfy stage2_impl invariants."""
    assert _blocked_scope.tier == "small"
    assert _blocked_scope.lines_actual == 0
    assert _FIXED_HEALTH.lowest_agent_confidence == "LOW"


# ---------------------------------------------------------------------------
# plan_missing is tested via build_task_message returning None (already above)
# ---------------------------------------------------------------------------


def test_synthesize_result_plan_missing_included_via_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """PLAN_MISSING constant is the expected reason string (compile-time check)."""
    assert PLAN_MISSING == "plan_missing"
    assert AIDER_NOT_FOUND == "aider_not_found"
