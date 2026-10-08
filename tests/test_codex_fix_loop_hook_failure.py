"""Tests for cw.codex_fix_loop.hook_failure — commit-hook failure parks (#2633)."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop.fence import LEFT_STAGED_HINT
from cw.codex_fix_loop.hook_failure import (
    CommitHookFailedError,
    as_hook_failure,
    failed_hooks,
    hook_failure_breach,
    installed_hook,
    summarize_hook_output,
)
from cw.codex_fix_loop.posted_text import TRUNCATION_MARKER
from cw.codex_review import CODEX_FIX_HOOK_FAILED
from tests._codex_review_helpers import _install_pre_commit_hook, _write
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CMD = ["git", "commit", "-m", "msg"]


def _failure(
    stdout: str = "", stderr: str = "", hook: str = "pre-commit"
) -> CommitHookFailedError:
    return CommitHookFailedError(1, _CMD, output=stdout, stderr=stderr, hook=hook)


class TestSummarizeHookOutput:
    def test_caps_at_20_lines(self) -> None:
        out = summarize_hook_output("\n".join(f"l{i}" for i in range(50)))

        assert out.removesuffix(TRUNCATION_MARKER).splitlines() == [
            f"l{i}" for i in range(20)
        ]

    def test_caps_at_1000_chars(self) -> None:
        out = summarize_hook_output("q" * 5000)

        assert out == "q" * 1000 + TRUNCATION_MARKER

    def test_redacts_known_secret_shapes(self) -> None:
        secrets = [
            "ghp_" + "b" * 36,
            "sk-" + "c" * 24,
            "Authorization: Bearer abc123",
            "token=abc",
        ]

        out = summarize_hook_output("\n".join(f"found {s}" for s in secrets))

        for secret in secrets:
            assert secret not in out
        assert "abc123" not in out

    def test_redaction_runs_before_truncation(self) -> None:
        token = "ghp_" + "d" * 36

        out = summarize_hook_output("e" * 990 + token)

        assert "ghp_" not in out

    def test_secret_assignment_lines_are_withheld(self) -> None:
        out = summarize_hook_output('config.py:3 password = "hunter2"\nother')

        assert "hunter2" not in out
        assert "other" in out


class TestFailedHooks:
    def test_parses_pre_commit_framework_lines(self) -> None:
        output = (
            "ruff.....Failed\n- hook id: ruff\n- exit code: 1\n"
            "mypy.....Failed\n- hook id: mypy\n- exit code: 2\n"
        )

        assert failed_hooks(_failure(stdout=output)) == [("ruff", 1), ("mypy", 2)]

    def test_unparseable_output_falls_back_to_hook_script_name_and_commit_exit_code(
        self,
    ) -> None:
        assert failed_hooks(_failure(stdout="lint broke", hook="commit-msg")) == [
            ("commit-msg", 1)
        ]


class TestSecretScanner:
    @pytest.mark.parametrize(
        "hook_id",
        [
            "gitleaks",
            "detect-secrets",
            "trufflehog",
            "detect-private-key",
            "secretlint",
        ],
    )
    def test_scanner_hook_ids_post_name_and_exit_code_only(self, hook_id: str) -> None:
        output = f"- hook id: {hook_id}\n- exit code: 1\nleaked SECRET-ABC123\n"

        breach = hook_failure_breach(_failure(stdout=output), 1)

        assert hook_id in breach.details
        assert "exit code 1" in breach.details
        assert "SECRET-ABC123" not in breach.details

    def test_scanner_name_only_in_output_still_suppresses_excerpt(self) -> None:
        breach = hook_failure_breach(
            _failure(stdout="gitleaks: finding SECRET-ABC123"), 1
        )

        assert "SECRET-ABC123" not in breach.details
        assert "secret-scanning hook" in breach.details

    def test_non_scanner_lint_keeps_excerpt(self) -> None:
        breach = hook_failure_breach(_failure(stdout="E501 line too long"), 1)

        assert "E501 line too long" in breach.details


class TestHookPresent:
    def test_executable_hook_is_found(self, make_git_repo: Callable[..., Path]) -> None:
        repo = make_git_repo("hk-present")
        assert installed_hook(repo) is None
        _install_pre_commit_hook(repo, "#!/bin/sh\nexit 0\n")

        assert installed_hook(repo) == "pre-commit"

    def test_non_executable_hook_is_not_found(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("hk-noexec")
        _write(repo / ".git" / "hooks" / "commit-msg", "#!/bin/sh\nexit 1\n")

        assert installed_hook(repo) is None

    def test_relative_hooks_path_resolves_against_worktree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("hk-relative")
        hook = repo / "hk" / "commit-msg"
        _write(hook, "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        git_in(repo, "config", "core.hooksPath", "hk")

        assert installed_hook(repo) == "commit-msg"


class TestCommitHookFailed:
    def test_is_called_process_error_subclass_with_stdout_and_stderr(self) -> None:
        exc = _failure(stdout="out", stderr="err")

        assert isinstance(exc, subprocess.CalledProcessError)
        assert (exc.stdout, exc.stderr, exc.hook) == ("out", "err", "pre-commit")


class TestAsHookFailure:
    def _raw(self) -> subprocess.CalledProcessError:
        return subprocess.CalledProcessError(
            1, _CMD, output="o" * 3000 + "TAIL-OUT", stderr="TAIL-ERR"
        )

    def test_logs_full_uncapped_output_at_warning(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo("hk-log")
        _install_pre_commit_hook(repo, "#!/bin/sh\nexit 1\n")

        with caplog.at_level(logging.WARNING, logger="cw.codex_fix_loop.hook_failure"):
            out = as_hook_failure(repo, self._raw(), 2)

        assert isinstance(out, CommitHookFailedError)
        assert "TAIL-OUT" in caplog.text
        assert "TAIL-ERR" in caplog.text

    def test_warning_names_logger_and_argument_sources(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo("hk-log-args")
        _install_pre_commit_hook(repo, "#!/bin/sh\nexit 1\n")
        exc = self._raw()

        with caplog.at_level(logging.WARNING, logger="cw.codex_fix_loop.hook_failure"):
            as_hook_failure(repo, exc, 2)

        [record] = caplog.records
        assert record.name == "cw.codex_fix_loop.hook_failure"
        assert record.levelname == "WARNING"
        assert record.args == (2, 1, " ".join(_CMD), exc.stdout, "TAIL-ERR")

    def test_no_hook_installed_reraises_plain_error(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        exc = self._raw()

        assert as_hook_failure(make_git_repo("hk-none"), exc, 1) is exc


class TestHookFailureBreach:
    def test_reason_details_and_hint(self) -> None:
        output = "- hook id: ruff\n- exit code: 1\nE501 too long\n"

        breach = hook_failure_breach(_failure(stdout=output), 3)

        assert breach.reason == CODEX_FIX_HOOK_FAILED
        assert "hook: ruff" in breach.details
        assert "exit code 1" in breach.details
        assert ".cw/codex_driver.log" in breach.details
        assert LEFT_STAGED_HINT in breach.recovery_hint
