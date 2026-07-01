"""Tests for cw.local_runner — AiderRunner + git synthesis (RFC 0005 F3)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from cw.auto_dev_result import AutoDevResult
from cw.local_runner import (
    _FIXED_HEALTH,
    AIDER_NO_OUTPUT,
    AIDER_NOT_FOUND,
    PLAN_MISSING,
    FakeAiderRunner,
    FakePlanFetcher,
    RealAiderRunner,
    _blocked_scope,
    aider_available,
    build_argv,
    build_env,
    build_task_message,
    read_process_start_time_ns,
    synthesize_git_result,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Fixtures: inherit tmp_config_dir autouse (redirects state paths).
# make_git_repo creates a real git repo for synthesize_git_result tests.
# ---------------------------------------------------------------------------


def _make_task(ticket_id: str = "T-1", scope_hint: str | None = None) -> MagicMock:
    task = MagicMock()
    task.ticket_id = ticket_id
    task.scope_hint = scope_hint
    return task


# ---------------------------------------------------------------------------
# FakeAiderRunner — records the launch call; returns a live sleep process
# ---------------------------------------------------------------------------


def test_fake_runner_launch_records_call_and_returns_live_proc(tmp_path: Path) -> None:
    """FakeAiderRunner.launch() records argv/cwd/env and returns a live process."""
    runner = FakeAiderRunner()
    argv = ["aider", "--model", "openai/test", "--message", "do stuff"]
    env = {"OPENAI_API_BASE": "http://localhost:1234/v1", "OPENAI_API_KEY": "local"}

    proc = runner.launch(tmp_path, argv, env)
    try:
        assert len(runner.calls) == 1
        call = runner.calls[0]
        assert call["argv"] == argv
        assert call["cwd"] == tmp_path
        assert call["env"] == env
        # The returned process is alive (a real 'sleep 60').
        assert proc.poll() is None
        assert read_process_start_time_ns(proc.pid) is not None
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# RealAiderRunner — fire-and-forget subprocess launch
# ---------------------------------------------------------------------------


def test_real_runner_launch_returns_live_popen_with_devnull(tmp_path: Path) -> None:
    """RealAiderRunner.launch() returns a live Popen wired to DEVNULL, no wait."""
    runner = RealAiderRunner()
    proc = runner.launch(tmp_path, ["sleep", "60"], dict(os.environ))
    try:
        # Fire-and-forget: the process is running and launch did not block on it.
        assert proc.poll() is None
        # stdout/stderr are DEVNULL (not PIPE), so no pipe handles are exposed.
        assert proc.stdout is None
        assert proc.stderr is None
    finally:
        proc.kill()
        proc.wait()


def test_real_runner_launch_raises_on_missing_binary(tmp_path: Path) -> None:
    """RealAiderRunner.launch() propagates FileNotFoundError for an absent binary."""
    import pytest

    runner = RealAiderRunner()
    with pytest.raises(FileNotFoundError):
        runner.launch(tmp_path, ["aider-nonexistent-binary-xyz"], {})


# ---------------------------------------------------------------------------
# read_process_start_time_ns
# ---------------------------------------------------------------------------


def test_read_start_time_ns_live_process() -> None:
    """read_process_start_time_ns returns a positive int for a live process."""
    proc = subprocess.Popen(
        ["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        start = read_process_start_time_ns(proc.pid)
        assert start is not None
        assert start > 0
    finally:
        proc.kill()
        proc.wait()


def test_read_start_time_ns_dead_pid_returns_none() -> None:
    """read_process_start_time_ns returns None for a PID with no /proc entry."""
    proc = subprocess.Popen(
        ["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    proc.wait()
    # Reap complete; a PID this high is almost certainly free on a test host.
    assert read_process_start_time_ns(2_000_000_000) is None


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


def test_build_env_does_not_forward_secrets() -> None:
    """build_env excludes operator shell secrets via the allowlist."""
    with patch.dict(
        os.environ,
        {"AWS_SECRET_ACCESS_KEY": "shhh", "GITHUB_TOKEN": "ghp_xxx"},
        clear=False,
    ):
        env = build_env("http://localhost:1234/v1")
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env


def test_build_env_forwards_aider_vars() -> None:
    """build_env forwards AIDER_* vars into the subprocess env."""
    with patch.dict(
        os.environ,
        {"AIDER_MODEL": "gpt-4o", "AIDER_SOMETHING_NEW": "1"},
        clear=False,
    ):
        env = build_env("http://localhost:1234/v1")
    assert env["AIDER_MODEL"] == "gpt-4o"
    assert env["AIDER_SOMETHING_NEW"] == "1"


def test_build_env_always_sets_openai_keys() -> None:
    """build_env always sets OPENAI_API_BASE and OPENAI_API_KEY."""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "real-key"}, clear=False):
        env = build_env("http://localhost:1234/v1")
    assert env["OPENAI_API_BASE"] == "http://localhost:1234/v1"
    assert env["OPENAI_API_KEY"] == "real-key"


def test_build_env_forwards_git_identity() -> None:
    """build_env forwards git identity vars required for aider commits."""
    with patch.dict(
        os.environ,
        {
            "GIT_AUTHOR_NAME": "Alice",
            "GIT_AUTHOR_EMAIL": "alice@example.com",
            "GIT_COMMITTER_NAME": "Alice",
            "GIT_COMMITTER_EMAIL": "alice@example.com",
        },
        clear=False,
    ):
        env = build_env("http://localhost:1234/v1")
    assert env["GIT_AUTHOR_NAME"] == "Alice"
    assert env["GIT_AUTHOR_EMAIL"] == "alice@example.com"
    assert env["GIT_COMMITTER_NAME"] == "Alice"
    assert env["GIT_COMMITTER_EMAIL"] == "alice@example.com"


def test_build_env_output_bounded_to_allowlist() -> None:
    """build_env output contains no keys outside allowlist + AIDER_* + OPENAI_*."""
    from cw.local_runner import _ENV_ALLOWLIST

    controlled_env = {
        "HOME": "/home/user",
        "PATH": "/usr/bin",
        "AIDER_MODEL": "gpt-4o",
        "AWS_SECRET_ACCESS_KEY": "shhh",
        "SLACK_BOT_TOKEN": "xoxb-xxx",
        "NPM_TOKEN": "npm_xxx",
        "GITHUB_TOKEN": "ghp_xxx",
    }
    with patch.dict(os.environ, controlled_env, clear=True):
        env = build_env("http://localhost:1234/v1")
    unexpected = {
        k
        for k in env
        if k not in _ENV_ALLOWLIST
        and not k.startswith("AIDER_")
        and k not in {"OPENAI_API_BASE", "OPENAI_API_KEY"}
    }
    assert not unexpected, f"Unexpected keys leaked into subprocess env: {unexpected}"


# ---------------------------------------------------------------------------
# aider_available
# ---------------------------------------------------------------------------


def test_aider_available_true_when_on_path() -> None:
    """aider_available returns True when the binary is on PATH."""
    with patch("cw.local_runner.shutil.which", return_value="/usr/bin/aider"):
        assert aider_available() is True


def test_aider_available_false_when_not_on_path() -> None:
    """aider_available returns False when the binary is absent."""
    with patch("cw.local_runner.shutil.which", return_value=None):
        assert aider_available() is False


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


def test_build_task_message_malformed_context_json(tmp_path: Path) -> None:
    """build_task_message returns plan-only message when context.json is malformed."""
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text("plan text", encoding="utf-8")
    (cw_dir / "context.json").write_text("{bad json", encoding="utf-8")

    result = build_task_message(tmp_path)

    assert result is not None
    assert "plan text" in result


# ---------------------------------------------------------------------------
# synthesize_git_result — git-only disposition paths
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


def test_synthesize_git_result_stage_complete(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """≥1 new commit since fork point → stage_complete."""
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

    task = _make_task(ticket_id="T-1", scope_hint="small")

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.status == "stage_complete"
    assert result.stage_reached == "stage2_impl"
    assert len(result.commits) >= 1
    assert result.scope.lines_actual is not None
    assert result.scope.tier == "small"
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_synthesize_git_result_blocked_no_output(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No new commits since fork point → blocked/aider_no_output."""
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
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == AIDER_NO_OUTPUT
    assert result.blocker.retry_eligible is True
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_synthesize_git_result_scope_tier_large(
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

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
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


def test_constant_values_plan_missing_aider_not_found(tmp_config_dir: Path) -> None:
    """PLAN_MISSING and AIDER_NOT_FOUND have the expected reason string values."""
    assert PLAN_MISSING == "plan_missing"
    assert AIDER_NOT_FOUND == "aider_not_found"


# ---------------------------------------------------------------------------
# FakePlanFetcher
# ---------------------------------------------------------------------------


def test_fake_plan_fetcher_records_calls(tmp_path: Path) -> None:
    """FakePlanFetcher.fetch() records every ticket_id passed to it."""
    fetcher = FakePlanFetcher(plan="plan text")
    fetcher.fetch("T-1")
    fetcher.fetch("T-2")
    assert fetcher.calls == ["T-1", "T-2"]


def test_fake_plan_fetcher_returns_configured_plan() -> None:
    """FakePlanFetcher returns the plan set at construction."""
    fetcher = FakePlanFetcher(plan="## my plan")
    assert fetcher.fetch("T-1") == "## my plan"


def test_fake_plan_fetcher_returns_none_when_no_plan() -> None:
    """FakePlanFetcher(plan=None) returns None from fetch()."""
    fetcher = FakePlanFetcher(plan=None)
    assert fetcher.fetch("T-1") is None


# ---------------------------------------------------------------------------
# build_task_message — tracker fallback
# ---------------------------------------------------------------------------


def test_build_task_message_fetches_from_tracker_when_plan_absent(
    tmp_path: Path,
) -> None:
    """build_task_message falls back to tracker when .cw/plan.md is absent."""
    plan_body = "## Implementation Plan\n\nDo the thing.\n<!-- plan-spec-reviewed -->"
    fetcher = FakePlanFetcher(plan=plan_body)

    result = build_task_message(tmp_path, ticket_id="896", plan_fetcher=fetcher)

    assert result is not None
    assert "Do the thing." in result
    assert fetcher.calls == ["896"]


def test_build_task_message_writes_plan_to_disk_after_fetch(
    tmp_path: Path,
) -> None:
    """Fetched plan is materialised to .cw/plan.md so retries skip the fetch."""
    plan_body = "fetched plan body <!-- plan-spec-reviewed -->"
    fetcher = FakePlanFetcher(plan=plan_body)

    build_task_message(tmp_path, ticket_id="896", plan_fetcher=fetcher)

    plan_path = tmp_path / ".cw" / "plan.md"
    assert plan_path.exists()
    assert plan_path.read_text(encoding="utf-8") == plan_body


def test_build_task_message_still_none_when_fetcher_returns_none(
    tmp_path: Path,
) -> None:
    """No plan.md, fetcher returns None (tracker has no plan) → returns None."""
    fetcher = FakePlanFetcher(plan=None)

    result = build_task_message(tmp_path, ticket_id="896", plan_fetcher=fetcher)

    assert result is None


def test_build_task_message_no_fetcher_no_ticket_returns_none(
    tmp_path: Path,
) -> None:
    """No plan.md, no fetcher, no ticket_id → None (unchanged baseline)."""
    result = build_task_message(tmp_path)
    assert result is None


def test_build_task_message_no_fetcher_with_ticket_returns_none(
    tmp_path: Path,
) -> None:
    """No plan.md, ticket_id provided but no fetcher → None (no fetch attempted)."""
    result = build_task_message(tmp_path, ticket_id="896")
    assert result is None


def test_build_task_message_plan_on_disk_skips_fetcher(tmp_path: Path) -> None:
    """When .cw/plan.md exists, fetcher is never called."""
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text("existing plan", encoding="utf-8")
    fetcher = FakePlanFetcher(plan="should not be called")

    result = build_task_message(tmp_path, ticket_id="896", plan_fetcher=fetcher)

    assert result is not None
    assert "existing plan" in result
    assert fetcher.calls == []


# ---------------------------------------------------------------------------
# GithubIssuePlanFetcher — delegates to fetch_approved_plan_comment
# ---------------------------------------------------------------------------


def test_github_issue_plan_fetcher_delegates_to_gh(tmp_path: Path) -> None:
    """GithubIssuePlanFetcher.fetch() delegates to fetch_approved_plan_comment."""
    from unittest.mock import patch

    from cw.local_runner import GithubIssuePlanFetcher

    fetcher = GithubIssuePlanFetcher()
    expected = "## Plan <!-- plan-spec-reviewed -->"

    with patch(
        "cw.local_runner.fetch_approved_plan_comment", return_value=expected
    ) as mock_fetch:
        result = fetcher.fetch("42")

    mock_fetch.assert_called_once_with("42")
    assert result == expected


def test_synthesize_git_result_threads_plan_source(tmp_path: Path) -> None:
    """synthesize_git_result passes plan_source through to AutoDevResult."""
    from unittest.mock import patch

    from cw.local_runner import _GitFacts, synthesize_git_result
    from cw.models import Stage, TicketTask

    fake_facts: _GitFacts = {
        "branch": "dev/test",
        "fork_point": "abc123",
        "commits": ["abc123"],
        "files": 1,
        "lines_actual": 10,
    }
    task = TicketTask(ticket_id="T-1", client="c", stage=Stage.IMPL)

    with patch("cw.local_runner._git_facts", return_value=fake_facts):
        result = synthesize_git_result(
            task=task,
            worktree=tmp_path,
            default_branch="main",
            plan_source="github_issue_existing",
        )

    assert result.status == "stage_complete"
    assert result.plan_source == "github_issue_existing"
