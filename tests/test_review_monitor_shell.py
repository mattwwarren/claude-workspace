"""Characterization tests for the review-monitor gh/git runners (#2499).

Covers ``_run_gh``, ``_run_git`` and the cached ``_get_our_username``
(moving to ``review_monitor_lib/shell.py``). ``subprocess.run`` is the faked
seam; the runners' own error handling runs for real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import _review_monitor_helpers as helpers


class _Recorder:
    """Stand-in for ``subprocess.run`` that records argv and replays one outcome."""

    def __init__(self, outcome: str | BaseException) -> None:
        self.outcome = outcome
        self.argvs: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: object) -> object:
        self.argvs.append(cmd)
        assert kwargs == {"capture_output": True, "text": True, "check": True}
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return subprocess.CompletedProcess(cmd, 0, stdout=self.outcome, stderr="")


def _install(
    monkeypatch: pytest.MonkeyPatch, outcome: str | BaseException
) -> _Recorder:
    recorder = _Recorder(outcome)
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


@pytest.mark.parametrize(
    ("runner", "kwargs", "expected_argv"),
    [
        ("_run_gh", {}, ["gh", "pr", "list"]),
        (
            "_run_gh",
            {"repo": "acme/widgets"},
            ["gh", "-R", "acme/widgets", "pr", "list"],
        ),
        ("_run_git", {}, ["git", "pr", "list"]),
        ("_run_git", {"cwd": "/repo"}, ["git", "-C", "/repo", "pr", "list"]),
    ],
)
def test_runner_builds_argv_and_strips_stdout(
    monkeypatch: pytest.MonkeyPatch,
    runner: str,
    kwargs: dict[str, str],
    expected_argv: list[str],
) -> None:
    recorder = _install(monkeypatch, "  out\n")

    assert helpers.get(runner)(["pr", "list"], **kwargs) == "out"
    assert recorder.argvs == [expected_argv]


@pytest.mark.parametrize(
    ("runner", "warning"),
    [("_run_gh", "gh CLI not found"), ("_run_git", "git not found in PATH")],
)
def test_runner_missing_binary_returns_empty_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    runner: str,
    warning: str,
) -> None:
    _install(monkeypatch, FileNotFoundError("no such binary"))

    with caplog.at_level("WARNING"):
        assert helpers.get(runner)(["status"]) == ""

    assert any(warning in r.message for r in caplog.records)


@pytest.mark.parametrize(("runner", "tool"), [("_run_gh", "gh"), ("_run_git", "git")])
def test_runner_failed_command_returns_empty_and_logs_stderr(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    runner: str,
    tool: str,
) -> None:
    failure = subprocess.CalledProcessError(1, [tool], stderr="  boom  \n")
    _install(monkeypatch, failure)

    with caplog.at_level("WARNING"):
        assert helpers.get(runner)(["status"]) == ""

    assert any(
        f"{tool} command failed: {tool} status\nboom" in r.message
        for r in caplog.records
    )


def test_get_our_username_queries_gh_once_and_caches(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = helpers.FakeCommands().add(("gh", "api", "user"), " matt-w \n")
    fake.install(monkeypatch)
    get_username = helpers.get("_get_our_username")

    assert get_username() == "matt-w"
    assert get_username() == "matt-w"
    assert fake.argvs() == [("gh", "api", "user", "--jq", ".login")]


def test_get_our_username_failure_is_empty(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers.FakeCommands().add(("gh",), "").install(monkeypatch)

    assert helpers.get("_get_our_username")() == ""
