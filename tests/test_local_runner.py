"""Tests for cw.local_runner — AiderRunner + git synthesis (RFC 0005 F3)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.executor_diagnostics import (
    ExecutorFailure,
    diagnostics_bundle_dir,
    render_bundle_path,
)
from cw.local_runner import (
    _FIXED_HEALTH,
    _PATH_FREE_TASK_INSTRUCTION,
    AIDER_FILE_REQUEST_UNANSWERED,
    AIDER_NO_OUTPUT,
    AIDER_NOT_FOUND,
    PLAN_MISSING,
    TASK_CONTEXT_RELATIVE_PATH,
    FakeAiderRunner,
    FakePlanFetcher,
    RealAiderRunner,
    _blocked_scope,
    _detect_unanswered_file_request,
    aider_available,
    build_aiderignore,
    build_argv,
    build_env,
    build_task_message,
    read_process_start_time_ns,
    synthesize_git_result,
)
from tests.conftest import commit_tracked_file

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


def test_real_runner_launch_writes_child_output_to_log_file(tmp_path: Path) -> None:
    """RealAiderRunner.launch() redirects child stdout/stderr to .cw/aider.log."""
    runner = RealAiderRunner()
    proc = runner.launch(tmp_path, ["sh", "-c", "echo hi"], dict(os.environ))
    proc.wait()

    log_path = tmp_path / ".cw" / "aider.log"
    assert log_path.exists()
    assert "hi" in log_path.read_text(encoding="utf-8")


def test_real_runner_launch_raises_on_missing_binary(tmp_path: Path) -> None:
    """RealAiderRunner.launch() propagates FileNotFoundError for an absent binary."""

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
    """read_process_start_time_ns returns None for a PID with no live process."""
    proc = subprocess.Popen(
        ["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    proc.wait()
    # Reap complete; a PID this high is almost certainly free on a test host.
    assert read_process_start_time_ns(2_000_000_000) is None


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


_READ_ONLY_PATH = Path(".cw/task_context.md")


def test_build_argv_prepends_openai_prefix() -> None:
    """build_argv prepends 'openai/' to a model without it."""
    argv = build_argv("qwen2.5-coder-32b-instruct", "task", [], _READ_ONLY_PATH)
    assert "--model" in argv
    model = argv[argv.index("--model") + 1]
    assert model == "openai/qwen2.5-coder-32b-instruct"


def test_build_argv_no_double_prefix() -> None:
    """build_argv does not double-prepend 'openai/'."""
    argv = build_argv("openai/qwen2.5-coder-32b-instruct", "task", [], _READ_ONLY_PATH)
    model = argv[argv.index("--model") + 1]
    assert model == "openai/qwen2.5-coder-32b-instruct"
    assert model.count("openai/") == 1


def test_build_argv_includes_required_flags() -> None:
    """build_argv includes all required aider flags."""
    argv = build_argv("model", "my task", [], _READ_ONLY_PATH)
    assert "--yes" in argv
    assert "--auto-commits" in argv
    assert "--no-pretty" in argv
    assert "--no-browser" in argv
    assert "--no-auto-lint" in argv
    assert "--no-auto-test" in argv
    assert "--no-stream" in argv
    assert "--map-tokens" in argv


def test_build_argv_message_value_is_redactable_shape() -> None:
    """The --message flag is immediately followed by its value, so redact_argv
    can replace that value wholesale by index (#1239)."""
    argv = build_argv("model", "ticket + plan body", [], _READ_ONLY_PATH)
    assert "--message" in argv
    idx = argv.index("--message")
    assert argv[idx + 1] == "ticket + plan body"


def test_build_argv_emits_file_flags_for_each_planned_file() -> None:
    """Each plan-enumerated file becomes a ``--file <path>`` pair, in order,
    positioned before --message (#1905)."""
    argv = build_argv("model", "instr", ["a.py", "b/c.py"], _READ_ONLY_PATH)
    assert argv.count("--file") == 2
    first = argv.index("--file")
    assert argv[first : first + 4] == ["--file", "a.py", "--file", "b/c.py"]
    assert first < argv.index("--message")


def test_build_argv_no_file_flags_when_files_empty() -> None:
    """files=[] emits no --file flag at all (backward-compat fallback pin)."""
    argv = build_argv("model", "instr", [], _READ_ONLY_PATH)
    assert "--file" not in argv


def test_build_argv_file_flags_do_not_disturb_message_redactable_shape() -> None:
    """--message is still immediately followed by its value with --file present."""
    argv = build_argv("model", "instr", ["a.py", "b/c.py"], _READ_ONLY_PATH)
    assert argv[argv.index("--message") + 1] == "instr"


def test_build_argv_emits_read_flag_for_task_context_path() -> None:
    """The task-context file is registered read-only via --read, positioned
    after the --file pairs and immediately before --message (#1905)."""
    argv = build_argv("m", "instr", ["a.py"], _READ_ONLY_PATH)
    assert argv == [
        "aider",
        "--model",
        "openai/m",
        "--file",
        "a.py",
        "--read",
        ".cw/task_context.md",
        "--message",
        "instr",
        "--yes",
        "--auto-commits",
        "--no-pretty",
        "--no-browser",
        "--no-auto-lint",
        "--no-auto-test",
        "--map-tokens",
        "0",
        "--no-stream",
    ]


def test_build_argv_read_flag_present_even_when_files_empty() -> None:
    """--read is unconditional (unlike --file): build_task_message always
    materialises the read-only file when it returns non-None."""
    argv = build_argv("m", "instr", [], _READ_ONLY_PATH)
    assert argv[argv.index("--read") + 1] == ".cw/task_context.md"


def test_build_argv_read_flag_does_not_disturb_message_redactable_shape() -> None:
    """--message keeps its index-adjacent value with both --file and --read."""
    argv = build_argv("m", "instr", ["a.py"], _READ_ONLY_PATH)
    assert argv[argv.index("--message") + 1] == "instr"


def test_build_argv_emits_aiderignore_flag_when_path_given() -> None:
    """A non-None aiderignore_path becomes a --aiderignore flag, grouped with
    the --file flags, before --read/--message (#1915)."""
    aiderignore_path = Path(".cw/aiderignore")
    argv = build_argv("m", "instr", ["a.py"], _READ_ONLY_PATH, aiderignore_path)
    assert "--aiderignore" in argv
    idx = argv.index("--aiderignore")
    assert argv[idx + 1] == str(aiderignore_path)
    assert idx < argv.index("--read")
    assert argv[argv.index("--message") + 1] == "instr"


def test_build_argv_omits_aiderignore_flag_when_none() -> None:
    """aiderignore_path=None (the default) emits no --aiderignore flag at all,
    preserving every pre-#1915 positional call site's argv unchanged (#1915)."""
    argv = build_argv("m", "instr", ["a.py"], _READ_ONLY_PATH)
    assert "--aiderignore" not in argv


# ---------------------------------------------------------------------------
# build_aiderignore (#1915) — closes the reflection-loop echo residual vector:
# blocks every git-tracked file outside the plan's manifest from aider's
# addable-file universe, so no model-reply echo of an excluded path can
# trigger check_for_file_mentions' auto-add under --yes-always.
# ---------------------------------------------------------------------------


def test_build_aiderignore_blocks_tracked_files_outside_manifest(
    make_git_repo: Callable[[str], Path],
) -> None:
    """Tracked files outside the manifest are blocked; manifest files are not."""
    worktree = make_git_repo("wt-aiderignore-block")
    commit_tracked_file(worktree, "core/database.py")
    commit_tracked_file(worktree, "src/in_scope.py")

    result = build_aiderignore(worktree, ["src/in_scope.py"])

    assert result is not None
    lines = result.read_text(encoding="utf-8").splitlines()
    assert "/core/database.py" in lines
    assert "/src/in_scope.py" not in lines


def test_build_aiderignore_returns_none_for_empty_manifest(
    make_git_repo: Callable[[str], Path],
) -> None:
    """An empty manifest degrades to no --aiderignore at all (#1905's fallback
    contract: absent manifest → unconstrained behaviour, never a hard block)."""
    worktree = make_git_repo("wt-aiderignore-empty-manifest")

    assert build_aiderignore(worktree, []) is None


def test_build_aiderignore_root_anchors_every_blocked_line(
    make_git_repo: Callable[[str], Path],
) -> None:
    """Every blocked and negated line is written with a leading '/' (or '!/'
    for negations) so a bare same-basename match in another directory doesn't
    spuriously match anywhere in the tree (gitignore's
    no-internal-slash-matches-anywhere gotcha)."""
    worktree = make_git_repo("wt-aiderignore-anchor")
    commit_tracked_file(worktree, "a/util.py")
    commit_tracked_file(worktree, "b/util.py")

    result = build_aiderignore(worktree, ["a/util.py"])

    assert result is not None
    lines = [
        line
        for line in result.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert lines
    for line in lines:
        assert line.startswith(("/", "!/"))


def test_build_aiderignore_merges_existing_repo_aiderignore(
    make_git_repo: Callable[[str], Path],
) -> None:
    """A pre-existing client-repo .aiderignore is folded in, not replaced —
    --aiderignore is a single-value override, so silently dropping it would
    regress any client repo relying on it (#1915)."""
    worktree = make_git_repo("wt-aiderignore-merge")
    (worktree / ".aiderignore").write_text("*.log\n", encoding="utf-8")
    commit_tracked_file(worktree, "core/database.py")
    commit_tracked_file(worktree, "src/in_scope.py")

    result = build_aiderignore(worktree, ["src/in_scope.py"])

    assert result is not None
    content = result.read_text(encoding="utf-8")
    assert "*.log" in content
    assert "/core/database.py" in content


def test_build_aiderignore_never_blocks_a_manifest_path_even_if_repo_aiderignore_would(
    make_git_repo: Callable[[str], Path],
) -> None:
    """The single most safety-critical assertion: even when the client repo's
    own .aiderignore would already match a manifest path, the generated file's
    *effective* gitignore matching still leaves the manifest path un-ignored —
    aider's --file intake loop silently *skips* (only a tool_warning, no
    error) any explicit --file entry that matches the aiderignore spec
    (coders/base_coder.py:449-457), so a manifest path matched by ANY pattern
    in the merged file — cw's own or carried over from the pre-existing repo
    .aiderignore — would silently defeat #1905's --file manifest feature.

    Checks git's own gitignore matcher against the merged file (via
    core.excludesFile), not just a literal-string search, so a pattern merged
    in verbatim from the pre-existing repo .aiderignore (e.g. src/*.py) can't
    silently re-exclude the manifest path while cw's own block-line format
    happens to be absent."""
    worktree = make_git_repo("wt-aiderignore-never-blocks-manifest")
    (worktree / ".aiderignore").write_text("src/*.py\n", encoding="utf-8")
    commit_tracked_file(worktree, "src/real_target.py")
    commit_tracked_file(worktree, "core/database.py")

    result = build_aiderignore(worktree, ["src/real_target.py"])

    assert result is not None
    lines = result.read_text(encoding="utf-8").splitlines()
    assert "/src/real_target.py" not in lines
    assert "!/src/real_target.py" in lines

    check = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            f"core.excludesFile={result}",
            "check-ignore",
            "--no-index",
            "-q",
            "src/real_target.py",
        ],
        capture_output=True,
        check=False,
    )
    assert check.returncode == 1, "manifest path must NOT be effectively ignored"


def test_build_aiderignore_fails_open_when_git_ls_files_errors(
    make_git_repo: Callable[[str], Path],
) -> None:
    """A failing 'git ls-files' degrades to None (no --aiderignore emitted),
    never a raised exception — _local_preflight runs outside spawn()'s
    try/except, so any exception here would propagate uncaught (#1915)."""
    worktree = make_git_repo("wt-aiderignore-git-fails")

    with patch(
        "cw.local_runner.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["git", "ls-files"]),
    ):
        result = build_aiderignore(worktree, ["src/in_scope.py"])

    assert result is None


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


def _task_context(worktree: Path) -> str:
    """Read the materialised read-only task-context file (#1905)."""
    return (worktree / TASK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")


# The ticket's own reproduction shape: a plan whose prose names paths the
# implementation must NOT edit. Hand-authored (this is *our* plan format, not
# an external system's payload), matching the conftest _plan_text convention.
_EXCLUSION_PLAN = """## Summary

Rework the staleness monitor.

**EXPLICITLY OUT OF SCOPE — do not edit even if it is present in your \
context:** `core/database.py`, `etl_mcp/api/document_extract.py`, \
`tests/pipelines_v2/test_community_home_medical_ingest_integration.py`

## Touch-point Contract
- `core/database.py:1380-1381` for a column type
- `core/models/auth.py:5-13` for a TypedDict shape

## Files Modified
- src/real_target.py
"""


def test_build_task_message_reads_plan(tmp_path: Path) -> None:
    """The plan reaches the model via the read-only file, not the message.

    Post-#1905 the returned task_message is the fixed path-free instruction;
    the plan body itself is materialised to TASK_CONTEXT_RELATIVE_PATH.
    """
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text("Do the thing.\n", encoding="utf-8")

    result = build_task_message(tmp_path)

    assert result == _PATH_FREE_TASK_INSTRUCTION
    assert "Do the thing." in _task_context(tmp_path)


def test_build_task_message_supplements_context(tmp_path: Path) -> None:
    """Ticket title/body from .cw/context.json land in the read-only file."""
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text("plan content", encoding="utf-8")
    (cw_dir / "context.json").write_text(
        json.dumps({"title": "My Ticket", "body": "Ticket body"}),
        encoding="utf-8",
    )

    result = build_task_message(tmp_path)

    assert result == _PATH_FREE_TASK_INSTRUCTION
    context = _task_context(tmp_path)
    assert "My Ticket" in context
    assert "Ticket body" in context
    assert "plan content" in context


def test_build_task_message_malformed_context_json(tmp_path: Path) -> None:
    """A malformed context.json still yields a plan-only read-only file."""
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text("plan text", encoding="utf-8")
    (cw_dir / "context.json").write_text("{bad json", encoding="utf-8")

    result = build_task_message(tmp_path)

    assert result == _PATH_FREE_TASK_INSTRUCTION
    assert "plan text" in _task_context(tmp_path)


def test_build_task_message_is_path_free_even_with_exclusion_list_and_touch_points(
    tmp_path: Path,
) -> None:
    """The regression test for #1905's over-inclusion half.

    aider scans the --message string for path-like tokens and (under
    --yes-always) auto-adds every one it finds to the chat — so an exclusion
    list or a touch-point citation in the plan prose used to force exactly the
    files it named *not* to touch into the edit set. The returned message must
    therefore carry no repo path at all.
    """
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text(_EXCLUSION_PLAN, encoding="utf-8")

    result = build_task_message(tmp_path)

    assert result is not None
    for path in (
        "core/database.py",
        "etl_mcp",
        "auth.py",
        "community_home_medical",
        "src/real_target.py",
    ):
        assert path not in result
    assert "/" not in result


def test_build_task_message_materializes_task_context_file_with_full_plan_and_header(
    tmp_path: Path,
) -> None:
    """Nothing is lost: the full plan text still reaches the model, relocated
    into the read-only reference file."""
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text(_EXCLUSION_PLAN, encoding="utf-8")
    (cw_dir / "context.json").write_text(
        json.dumps({"title": "Ticket title", "body": "Ticket body"}),
        encoding="utf-8",
    )

    build_task_message(tmp_path)

    context = _task_context(tmp_path)
    assert "core/database.py" in context
    assert "src/real_target.py" in context
    assert "Ticket title" in context
    assert "Ticket body" in context


def test_build_task_message_task_context_file_overwritten_on_each_call(
    tmp_path: Path,
) -> None:
    """A retry into the same worktree must not read a prior attempt's plan."""
    cw_dir = tmp_path / ".cw"
    cw_dir.mkdir()
    (cw_dir / "plan.md").write_text("first plan", encoding="utf-8")
    build_task_message(tmp_path)

    (cw_dir / "plan.md").write_text("second plan", encoding="utf-8")
    build_task_message(tmp_path)

    context = _task_context(tmp_path)
    assert "second plan" in context
    assert "first plan" not in context


def test_build_task_message_returns_none_still_skips_task_context_write(
    tmp_path: Path,
) -> None:
    """The plan-missing blocked path leaves no orphaned read-only file."""
    assert build_task_message(tmp_path) is None
    assert not (tmp_path / TASK_CONTEXT_RELATIVE_PATH).exists()


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
    assert result.health.lowest_agent_confidence == "MEDIUM"
    assert result.health.any_incomplete_risk is True
    assert result.health.recommendation == "EXIT_FOR_HUMAN_REVIEW"
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def _make_no_output_worktree(make_git_repo: Callable[[str], Path], name: str) -> Path:
    """Helper: git repo with a fetched origin/main and no new commits."""
    worktree = make_git_repo(name)
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
    return worktree


def _write_aider_log(worktree: Path, content: bytes | str) -> None:
    """Helper: write .cw/aider.log under worktree, creating .cw/ if needed."""
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    log_path = cw_dir / "aider.log"
    if isinstance(content, bytes):
        log_path.write_bytes(content)
    else:
        log_path.write_text(content, encoding="utf-8")


def test_synthesize_git_result_blocked_no_output(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No new commits since fork point → blocked/aider_no_output."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-no-output")
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
    assert result.blocker.details == ""
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_synthesize_git_result_no_output_with_log_populates_details(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No commits + populated aider.log → blocker.details carries the log tail."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-no-output-log")
    _write_aider_log(worktree, "aider: some diagnostic output\n")
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.blocker is not None
    assert "some diagnostic output" in result.blocker.details


def test_synthesize_git_result_no_output_persists_diagnostics(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """AIDER_NO_OUTPUT path persists a missing_output bundle using the aider.log
    tail as stdout_excerpt when session_id is provided (#1239)."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-no-output-diag")
    _write_aider_log(worktree, "aider: some diagnostic output\n")
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
        session_id="s-noout",
    )

    assert result.blocker is not None
    assert result.blocker.reason == AIDER_NO_OUTPUT
    assert result.blocker.details == (
        "aider: some diagnostic output\n"
        f" [diagnostics: {render_bundle_path('s-noout')}]"
    )
    # Filename now carries an occurred_at timestamp suffix (#1330 item 7).
    [path] = list(diagnostics_bundle_dir("s-noout").glob("aider-missing_output-*.json"))
    assert path.exists()
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.category == "missing_output"
    assert failure.executor_name == "aider"
    assert failure.reviewer_role is None
    assert "some diagnostic output" in failure.stdout_excerpt


def test_synthesize_git_result_no_output_no_session_id_skips_diagnostics(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Default session_id=None makes the diagnostics persist a no-op (#1239)."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-no-output-nodiag")
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )
    assert result.blocker is not None
    assert result.blocker.reason == AIDER_NO_OUTPUT
    # No session id → no diagnostics tree created at all.
    from cw.config import state_dir

    assert not (state_dir() / "sessions").exists()


def test_synthesize_git_result_no_output_no_log_empty_details(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No commits + no aider.log file → blocker.details is empty string."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-no-output-no-log")
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.blocker is not None
    assert result.blocker.details == ""


def test_synthesize_git_result_no_output_log_truncated_to_tail(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A log file over 4000 chars is truncated to exactly the last 4000 chars."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-no-output-truncate")
    long_output = "x" * 5000
    _write_aider_log(worktree, long_output)
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.blocker is not None
    assert result.blocker.details == long_output[-4000:]


def test_synthesize_git_result_no_output_malformed_utf8_replaced(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Invalid/truncated UTF-8 bytes in the log degrade via replacement, not raise."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-no-output-malformed")
    _write_aider_log(
        worktree,
        b"partial output before crash: \xff\xfe truncated multi-byte tail \xe2\x98",
    )
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.blocker is not None
    assert "partial output before crash:" in result.blocker.details


# ---------------------------------------------------------------------------
# _detect_unanswered_file_request — free-text "add this file" ask (#1905)
# ---------------------------------------------------------------------------

# Verbatim model output captured on the two real stalled runs the ticket cites.
_GEN5307_LOG = (
    "To implement this plan, I need to propose edits to one existing file "
    "that hasn't been\nadded to the chat yet:\n\n"
    "* tests/pipelines_v2/functions/test_pipeline_staleness_monitor_dedupe.py\n\n"
    "Please add this file to the chat so I can proceed with the full "
    "implementation.\n\nTokens: 116k sent, 2.0k received.\n"
)
_GEN5457_LOG = (
    "For files not in the chat, I need to tell the user their full path names "
    "and ask them to add the files to the chat.\n...\n"
    "I can only create SEARCH/REPLACE blocks for files that have been added "
    "to the chat.\n"
)
# aider's own post-add confirmation (aider/prompts.py:31-33) — must NOT match.
_ADDED_FILES_LOG = (
    "I added these files to the chat: core/database.py\n"
    "Let me know if there are others we should add."
)
_SEARCH_BLOCK = (
    "core/database.py\n<<<<<<< SEARCH\nold = 1\n=======\nnew = 2\n>>>>>>> REPLACE\n"
)


def test_detect_unanswered_file_request_true_for_ticket_body_quote() -> None:
    """The GEN-5307 stall's verbatim model output is classified as a file ask."""
    assert _detect_unanswered_file_request(_GEN5307_LOG) is True


def test_detect_unanswered_file_request_true_for_gen5457_quotes() -> None:
    """The GEN-5457 stall's verbatim model output is classified likewise."""
    assert _detect_unanswered_file_request(_GEN5457_LOG) is True


def test_detect_unanswered_file_request_false_when_search_block_present() -> None:
    """Edit blocks were emitted → a different failure; must not misclassify."""
    assert _detect_unanswered_file_request(_GEN5457_LOG + _SEARCH_BLOCK) is False


def test_detect_unanswered_file_request_false_for_successful_add_confirmation() -> None:
    """aider's own "I added these files to the chat" confirmation is not an ask."""
    assert _detect_unanswered_file_request(_ADDED_FILES_LOG) is False


def test_detect_unanswered_file_request_false_for_generic_empty_log() -> None:
    """Unrelated diagnostic output is not a file ask."""
    assert _detect_unanswered_file_request("aider: some diagnostic output\n") is False


def test_detect_unanswered_file_request_false_for_empty_string() -> None:
    """An empty log is not a file ask."""
    assert _detect_unanswered_file_request("") is False


def test_synthesize_git_result_no_output_file_request_disposition(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No commits + an unanswered file ask → the distinct parked disposition."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-file-request")
    _write_aider_log(worktree, _GEN5307_LOG)
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.blocker is not None
    assert result.blocker.reason == AIDER_FILE_REQUEST_UNANSWERED
    assert result.blocker.retry_eligible is False
    assert result.blocker.retry_delay_seconds is None
    assert "Please add this file to the chat" in result.blocker.details
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_synthesize_git_result_no_output_file_request_still_persists_diagnostics(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The file-request branch still writes a missing_output bundle (#1239)."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-file-request-diag")
    _write_aider_log(worktree, _GEN5307_LOG)
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
        session_id="s-filereq",
    )

    assert result.blocker is not None
    assert result.blocker.reason == AIDER_FILE_REQUEST_UNANSWERED
    assert render_bundle_path("s-filereq") in result.blocker.details
    [path] = list(
        diagnostics_bundle_dir("s-filereq").glob("aider-missing_output-*.json")
    )
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.category == "missing_output"


def test_synthesize_git_result_no_output_generic_reason_when_no_phrase_match(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Unrelated log content still yields the generic AIDER_NO_OUTPUT reason."""
    worktree = _make_no_output_worktree(make_git_repo, "wt-file-request-generic")
    _write_aider_log(worktree, "aider: some diagnostic output\n")
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.blocker is not None
    assert result.blocker.reason == AIDER_NO_OUTPUT
    assert result.blocker.retry_eligible is True


def test_synthesize_git_result_detects_file_request_beyond_tail_window(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The edit-block veto scans the FULL log, not the 4000-char details tail.

    A SEARCH marker that scrolled out of the tail window still means aider
    produced edits, so the run must not be classified as an unanswered ask.
    """
    worktree = _make_no_output_worktree(make_git_repo, "wt-file-request-beyond-tail")
    _write_aider_log(worktree, _SEARCH_BLOCK + "x" * 5000 + _GEN5307_LOG)
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.blocker is not None
    assert result.blocker.reason == AIDER_NO_OUTPUT


def test_synthesize_git_result_has_no_manifest_cross_check_by_design(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Intentional design boundary, not an open gap: synthesize_git_result does
    not cross-check committed files against the plan manifest, by design.

    #1915 closed the reflection-loop echo vector — a model reply that echoes
    an excluded path re-triggering ``check_for_file_mentions`` →
    ``confirm_ask`` auto-accept under --yes-always on every reflection round
    (base_coder.py:1561, independent of the --message/--read split #1905
    closed) — via a *prevention* fix upstream of this function:
    ``local_runner.build_aiderignore``/``build_argv`` narrow aider's own
    addable-file universe at argv-construction time (pre-flight), before aider
    ever starts, so an excluded file can never enter the chat in the first
    place. See test_build_aiderignore_* and
    test_local_executor_spawn_passes_aiderignore_flag_to_argv for the actual
    closure assertions.

    synthesize_git_result itself is a post-hoc, git-facts-only function this
    fix does not touch: scope carries a plain file *count*
    (schema.py:292), _git_facts → _parse_numstat_totals reduce the diff to
    counts, and synthesize_git_result takes no manifest parameter at all —
    committed file names are retained nowhere in AutoDevResult, so a post-hoc
    cross-check against the plan manifest remains not structurally possible
    with today's data shape. That is now an accepted design boundary, not a
    residual gap: prevention makes detection here unnecessary. This test pins
    that boundary so a future change to this function's contract is a
    deliberate decision, not an accidental regression.
    """
    worktree = _make_no_output_worktree(make_git_repo, "wt-reflection-echo")
    _write_plan(worktree)
    (worktree / ".cw" / "plan.md").write_text(
        "## Files Modified\n- src/in_scope.py\n", encoding="utf-8"
    )
    # The reflection-echo outcome: a file the manifest never named, committed.
    excluded = worktree / "core"
    excluded.mkdir(parents=True, exist_ok=True)
    (excluded / "database.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "core/database.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "echoed edit"],
        check=True,
        capture_output=True,
    )
    task = _make_task()

    result = synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch="main",
    )

    assert result.status == "stage_complete"
    assert result.blocker is None
    assert result.scope.files == 1


def test_real_runner_launch_truncates_log_on_retry(tmp_path: Path) -> None:
    """A second launch() call truncates the log, not appends to the prior attempt."""
    runner = RealAiderRunner()
    env = dict(os.environ)

    proc1 = runner.launch(tmp_path, ["sh", "-c", "echo first"], env)
    proc1.wait()

    proc2 = runner.launch(tmp_path, ["sh", "-c", "echo second"], env)
    proc2.wait()

    log_text = (tmp_path / ".cw" / "aider.log").read_text(encoding="utf-8")
    assert "second" in log_text
    assert "first" not in log_text


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

    assert result == _PATH_FREE_TASK_INSTRUCTION
    assert "Do the thing." in _task_context(tmp_path)
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

    assert result == _PATH_FREE_TASK_INSTRUCTION
    assert "existing plan" in _task_context(tmp_path)
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


# ----------------------------------------------------------------------
# #1487 — _git_facts delegates numstat parsing to cw.worktree
# ----------------------------------------------------------------------


def _git_run(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def test_git_facts_counts_files_and_lines_via_shared_parser(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Regression pin: _git_facts still totals numstat after the parser extraction."""
    from cw.local_runner import _git_facts

    worktree = make_git_repo("wt-1487-facts")
    _git_run(worktree, "remote", "add", "origin", str(worktree))
    _git_run(worktree, "fetch", "origin", "main")
    _git_run(worktree, "checkout", "-b", "dev/1487-facts")
    for i in range(2):
        (worktree / f"f{i}.txt").write_text("a\nb\nc\n", encoding="utf-8")
    _git_run(worktree, "add", "-A")
    _git_run(worktree, "commit", "-m", "two files")

    facts = _git_facts(worktree, "main")

    assert facts["files"] == 2
    assert facts["lines_actual"] == 6
    assert facts["fork_point"]


def test_git_facts_and_compute_branch_diff_scope_agree(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Both scope producers must report identical numbers for the same repo state."""
    from cw.local_runner import _git_facts
    from cw.worktree import compute_branch_diff_scope

    worktree = make_git_repo("wt-1487-agree")
    _git_run(worktree, "remote", "add", "origin", str(worktree))
    _git_run(worktree, "fetch", "origin", "main")
    _git_run(worktree, "checkout", "-b", "dev/1487-agree")
    (worktree / "only.txt").write_text("x\ny\n", encoding="utf-8")
    _git_run(worktree, "add", "-A")
    _git_run(worktree, "commit", "-m", "one file")

    facts = _git_facts(worktree, "main")
    measured = compute_branch_diff_scope(worktree, "main")

    assert measured is not None
    assert (facts["files"], facts["lines_actual"]) == (
        measured["files"],
        measured["lines_actual"],
    )
