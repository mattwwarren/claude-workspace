"""Tests for cw.codex_runner — CodexRunner seam (RFC 0005 F1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_runner import FakeCodexRunner, RealCodexRunner

if TYPE_CHECKING:
    from pathlib import Path


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
