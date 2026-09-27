"""Tests for cw.codex_runner — CodexRunner seam (RFC 0005 F1) and the detached
``cw codex run`` job launcher (RFC 0014 A2, #2388)."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import cw.codex_driver as codex_driver
from cw.codex_runner import (
    FakeCodexRunner,
    RealCodexJobRunner,
    RealCodexRunner,
    build_codex_run_argv,
    build_codex_run_env,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# build_codex_run_argv / build_codex_run_env / RealCodexJobRunner (#2388)
# ---------------------------------------------------------------------------


def test_build_codex_run_argv_includes_stage_review_and_session_id() -> None:
    """argv runs ``python -m cw codex run <ticket> --stage review --session-id``."""
    argv = build_codex_run_argv(
        ticket_id="T-9", session_id="sid-9", wall_clock_budget_seconds=None
    )

    assert argv[:5] == [sys.executable, "-m", "cw", "codex", "run"]
    assert argv[5] == "T-9"
    stage_idx = argv.index("--stage")
    assert argv[stage_idx + 1] == "review"
    sid_idx = argv.index("--session-id")
    assert argv[sid_idx + 1] == "sid-9"
    # The launcher hardcodes the literal rather than importing cw.codex_driver
    # (D-1); keep the two honest here.
    assert codex_driver.STAGE_REVIEW == "review"


def test_build_codex_run_argv_omits_wall_clock_flag_when_none() -> None:
    argv = build_codex_run_argv(
        ticket_id="T-9", session_id="sid-9", wall_clock_budget_seconds=None
    )
    assert "--wall-clock-budget-seconds" not in argv


def test_build_codex_run_argv_includes_wall_clock_flag_when_set() -> None:
    argv = build_codex_run_argv(
        ticket_id="T-9", session_id="sid-9", wall_clock_budget_seconds=120
    )
    assert argv[-2:] == ["--wall-clock-budget-seconds", "120"]


def test_build_codex_run_env_inherits_full_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike aider/opencode's allowlist, the job inherits the full environment."""
    monkeypatch.setenv("CW_TEST_CODEX_RUN_SENTINEL", "kept")

    env = build_codex_run_env()

    assert env["CW_TEST_CODEX_RUN_SENTINEL"] == "kept"
    assert env == dict(os.environ)


def test_real_codex_job_runner_launch_writes_to_codex_driver_log(
    tmp_path: Path,
) -> None:
    """RealCodexJobRunner.launch() redirects child output to .cw/codex_driver.log."""
    runner = RealCodexJobRunner()
    proc = runner.launch(tmp_path, ["sh", "-c", "echo hi"], dict(os.environ))
    proc.wait()

    log_path = tmp_path / ".cw" / "codex_driver.log"
    assert log_path.exists()
    assert "hi" in log_path.read_text(encoding="utf-8")


def test_real_codex_job_runner_passes_start_new_session(tmp_path: Path) -> None:
    """RealCodexJobRunner.launch() detaches the child into its own session."""
    runner = RealCodexJobRunner()
    with patch("cw.executor_launch.subprocess.Popen") as mock_popen:
        runner.launch(tmp_path, ["sh", "-c", "true"], {})
    assert mock_popen.call_args.kwargs["start_new_session"] is True
    assert mock_popen.call_args.kwargs["cwd"] == tmp_path


# ---------------------------------------------------------------------------
# FakeCodexRunner — records argv/cwd/timeout
# ---------------------------------------------------------------------------


def test_codex_runner_records_argv(tmp_path: Path) -> None:
    """FakeCodexRunner.run() records argv, cwd, and timeout per call."""
    runner = FakeCodexRunner(returncode=0, stdout="", stderr="")
    argv = ["codex", "exec", "review", "--base", "main"]

    runner.run(tmp_path, argv, 900)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"] == argv
    assert call["cwd"] == tmp_path
    assert call["timeout"] == 900


def test_fake_runner_returns_configured_result(tmp_path: Path) -> None:
    """FakeCodexRunner returns the configured returncode/stdout/stderr."""
    runner = FakeCodexRunner(returncode=1, stderr="some error")
    result = runner.run(tmp_path, ["codex"], None)
    assert result.returncode == 1
    assert result.stderr == "some error"
    assert result.timed_out is False


def test_fake_runner_simulate_timeout_flag(tmp_path: Path) -> None:
    """FakeCodexRunner with simulate_timeout=True returns timed_out=True."""
    runner = FakeCodexRunner(simulate_timeout=True)
    result = runner.run(tmp_path, ["codex"], 60)
    assert result.timed_out is True
    assert result.returncode == -1


def test_fake_runner_returns_output_file_content(tmp_path: Path) -> None:
    """FakeCodexRunner returns the configured output_file_content directly."""
    runner = FakeCodexRunner(output_file_content='{"x": 1}')
    result = runner.run(tmp_path, ["codex"], None)
    assert result.output_file_content == '{"x": 1}'


def test_fake_runner_records_stdin(tmp_path: Path) -> None:
    """FakeCodexRunner records the stdin passed per call (#1236)."""
    runner = FakeCodexRunner(returncode=0)
    runner.run(tmp_path, ["codex", "exec"], 60, stdin="reviewer prompt body")
    assert runner.calls[0]["stdin"] == "reviewer prompt body"


def test_fake_runner_records_none_stdin_by_default(tmp_path: Path) -> None:
    """FakeCodexRunner records stdin=None when the kwarg is omitted (#1236)."""
    runner = FakeCodexRunner(returncode=0)
    runner.run(tmp_path, ["codex", "exec"], 60)
    assert runner.calls[0]["stdin"] is None


# ---------------------------------------------------------------------------
# RealCodexRunner — subprocess handling
# ---------------------------------------------------------------------------


def test_run_codex_not_found(tmp_path: Path) -> None:
    """RealCodexRunner.run() catches FileNotFoundError when binary is absent."""
    runner = RealCodexRunner()
    result = runner.run(tmp_path, ["codex-nonexistent-binary-xyz"], None)
    assert not result.timed_out
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_run_codex_real_success(tmp_path: Path) -> None:
    """RealCodexRunner.run() returns returncode=0 and captures stdout on success."""
    runner = RealCodexRunner()
    result = runner.run(tmp_path, ["echo", "hello"], None)
    assert result.returncode == 0
    assert result.timed_out is False
    assert "hello" in result.stdout


def test_run_codex_timeout(tmp_path: Path) -> None:
    """RealCodexRunner.run() catches TimeoutExpired and sets timed_out=True."""
    runner = RealCodexRunner()
    # "sleep 60" will be killed by a 0-second timeout.
    result = runner.run(tmp_path, ["sleep", "60"], 0)
    assert result.timed_out is True


def test_real_runner_reads_output_file(tmp_path: Path) -> None:
    """RealCodexRunner.run() reads the file at the argv '-o' path after exit."""
    output_path = tmp_path / "output.json"
    output_path.write_text('{"x": 1}', encoding="utf-8")
    runner = RealCodexRunner()
    result = runner.run(tmp_path, ["echo", "hi", "-o", str(output_path)], None)
    assert result.output_file_content == '{"x": 1}'


def test_real_runner_missing_output_file_returns_none(tmp_path: Path) -> None:
    """RealCodexRunner.run() returns None when the '-o' path was never written."""
    missing_path = tmp_path / "missing-output.json"
    runner = RealCodexRunner()
    result = runner.run(tmp_path, ["echo", "hi", "-o", str(missing_path)], None)
    assert result.output_file_content is None


def test_real_runner_non_utf8_output_file_returns_none(tmp_path: Path) -> None:
    """RealCodexRunner.run() returns None when the '-o' file isn't valid UTF-8."""
    output_path = tmp_path / "output.json"
    output_path.write_bytes(b"\xff\xfe\x00\x01")
    runner = RealCodexRunner()
    result = runner.run(tmp_path, ["echo", "hi", "-o", str(output_path)], None)
    assert result.output_file_content is None


def test_real_runner_writes_stdin_to_process(tmp_path: Path) -> None:
    """RealCodexRunner.run() feeds stdin to the process (#1236)."""
    runner = RealCodexRunner()
    # `cat` echoes its stdin to stdout, proving the input reached the process.
    result = runner.run(tmp_path, ["cat"], None, stdin="hello from stdin")
    assert result.returncode == 0
    assert "hello from stdin" in result.stdout


def test_real_runner_no_stdin_is_backward_compatible(tmp_path: Path) -> None:
    """RealCodexRunner.run() with no stdin kwarg leaves stdin at /dev/null."""
    runner = RealCodexRunner()
    # `cat` with /dev/null stdin produces empty stdout and exits 0.
    result = runner.run(tmp_path, ["cat"], None)
    assert result.returncode == 0
    assert result.stdout == ""
