"""One bounded repair turn when a commit hook rejects a fix cycle (#2987).

The unit classes pin the pure classifier (:func:`repair_excerpt`), the one
repair line the park carries, and the repair prompt's contents. The
integration class drives the real loop over a real git repo with a real
pre-commit hook, so the repaired result is measured, committed and pushed
(or parked) by the same code a production cycle runs.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop import commit as commit_module
from cw.codex_fix_loop.commit import _build_repair_prompt
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT
from cw.codex_fix_loop.growth import DEFAULT_GROWTH_BUDGET_LINES
from cw.codex_fix_loop.hook_failure import (
    CommitHookFailedError,
    hook_failure_breach,
    repair_excerpt,
)
from cw.codex_review import CODEX_FIX_GROWTH_BUDGET, CODEX_FIX_HOOK_FAILED
from cw.codex_runner import CodexRunResult
from tests._codex_review_helpers import _install_pre_commit_hook
from tests.conftest import git_in
from tests.test_codex_fix_loop import (
    _CLEAN_DOC,
    _MF_DOC,
    _editor,
    _FixLoopRunner,
    _run_loop,
    _with_plan,
    _worktree,
)
from tests.test_codex_fix_loop_commit import _SENSITIVE, _blocker_reason, _heads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult

_LINT_LINE = "fix.py:1:89: E501 Line too long (120 > 88)"

_LINT_HOOK = (
    "#!/bin/sh\n"
    "if grep -q LINT_MARKER fix.py 2>/dev/null; then\n"
    f"  echo '{_LINT_LINE}'\n"
    "  exit 1\n"
    "fi\n"
    "exit 0\n"
)
_ALWAYS_FAILING_HOOK = f"#!/bin/sh\necho '{_LINT_LINE}'\nexit 1\n"
_MARKER_FIX = _editor("fix.py", "LINT_MARKER = 1\n")
_CLEAN_REPAIR = _editor("fix.py", "patched = 1\n")
_GROWTH_PLAN = "# Plan\n\n## Files Modified\n\n- `fix.py`\n"
_GROWTH_REPAIR = _editor("fix.py", "".join(f"r{i} = {i}\n" for i in range(8)))


def _failure(
    output: str,
    *,
    hook: str = "pre-commit",
    returncode: int = 1,
) -> CommitHookFailedError:
    return CommitHookFailedError(
        returncode,
        ["git", "commit", "-m", "x"],
        output=output,
        stderr="",
        hook=hook,
    )


_MEASURED = ("fix.py",)


class TestRepairExcerpt:
    def test_lint_error_with_measured_path_returns_excerpt(self) -> None:
        excerpt = repair_excerpt(_failure(_LINT_LINE), _MEASURED)
        assert excerpt is not None
        assert "E501" in excerpt

    def test_commit_msg_hook_gets_no_repair(self) -> None:
        exc = _failure(_LINT_LINE, hook="commit-msg")
        assert repair_excerpt(exc, _MEASURED) is None

    def test_secret_scanner_gets_no_repair_and_is_not_echoed(self) -> None:
        output = "- hook id: gitleaks\n- exit code: 1\nfix.py leaked SECRET-ABC\n"
        assert repair_excerpt(_failure(output), _MEASURED) is None

    def test_framework_exit_codes_126_and_127_get_no_repair(self) -> None:
        for code in (126, 127):
            output = f"- hook id: ruff\n- exit code: {code}\nfix.py\n"
            assert repair_excerpt(_failure(output), _MEASURED) is None

    def test_environment_markers_get_no_repair_even_with_a_path(self) -> None:
        markers = (
            "/bin/sh: ruff: command not found",
            "Executable `ruff` not found",
            "pre-commit not found",
            "fix.py: Permission denied",
            "An unexpected error has occurred: fix.py",
            "Traceback (most recent call last):\nfix.py",
            "ModuleNotFoundError: fix",
            "exec format error: fix.py",
            "fix.py: No such file or directory",
        )
        for output in markers:
            assert repair_excerpt(_failure(output), _MEASURED) is None, output

    def test_plain_script_missing_binary_is_caught_by_output_not_exit_code(
        self,
    ) -> None:
        output = (
            ".git/hooks/pre-commit: line 2: /nonexistent/bin/ruff: "
            "No such file or directory\n"
        )
        assert repair_excerpt(_failure(output, returncode=1), ("fix.py",)) is None

    def test_no_measured_path_in_excerpt_gets_no_repair(self) -> None:
        assert repair_excerpt(_failure("LINT-ERROR-XYZ"), _MEASURED) is None

    def test_path_beyond_the_20_line_cap_gets_no_repair(self) -> None:
        filler = "\n".join(f"noise {i}" for i in range(25))
        output = f"{filler}\n{_LINT_LINE}\n"
        assert repair_excerpt(_failure(output), _MEASURED) is None

    def test_path_match_respects_boundaries(self) -> None:
        output = "prefix.py:1:1: E501 Line too long\n"
        assert repair_excerpt(_failure(output), ("fix.py",)) is None

    def test_excerpt_withholds_secret_lines(self) -> None:
        output = f"{_LINT_LINE}\nAKIA-SECRET-LINE token=abc\n"
        excerpt = repair_excerpt(_failure(output), _MEASURED)
        assert excerpt is not None
        assert "fix.py:1:89" in excerpt


class TestRepairLine:
    _BASE = (
        "codex fix cycle 1 produced changes, but `git commit` was rejected by a "
        "commit hook (hook: pre-commit, exit code 1) after one re-stage retry."
    )

    def _details(self, repair: str | None) -> str:
        exc = _failure(_LINT_LINE)
        if repair is None:
            breach = hook_failure_breach(exc, 1)
        else:
            breach = hook_failure_breach(exc, 1, repair=repair)  # type: ignore[arg-type]
        return breach.details

    def test_default_says_not_attempted(self) -> None:
        assert "Repair turn: not attempted." in self._details(None)

    def test_turn_failed_line(self) -> None:
        details = self._details("turn_failed")
        assert "Repair turn: attempted once; the fix agent's repair turn failed." in (
            details
        )

    def test_rejected_line(self) -> None:
        details = self._details("rejected")
        assert "Repair turn: attempted once; the commit was rejected again." in details

    def test_exactly_one_repair_line(self) -> None:
        for repair in ("not_attempted", "turn_failed", "rejected"):
            details = self._details(repair)
            assert details.count("Repair turn:") == 1

    def test_existing_details_and_hint_unchanged(self) -> None:
        breach = hook_failure_breach(_failure(_LINT_LINE), 1, repair="rejected")
        assert self._BASE in breach.details
        assert breach.recovery_hint is not None
        assert LEFT_STAGED_HINT in breach.recovery_hint

    def test_secret_scanner_branch_also_carries_the_line(self) -> None:
        exc = _failure("- hook id: gitleaks\n- exit code: 1\nSECRET-ABC\n")
        breach = hook_failure_breach(exc, 1, repair="turn_failed")
        assert "SECRET-ABC" not in breach.details
        assert "Repair turn: attempted once" in breach.details


class TestRepairPrompt:
    def test_prompt_carries_only_hook_output_and_standing_limits(self) -> None:
        prompt = _build_repair_prompt(
            "fix.py:1:89: E501 Line too long",
            cycle=3,
            standing_sections=("## Scope Rules\n- rule", "## Files You May Change"),
        )
        assert prompt.startswith("# Codex Fix Cycle 3")
        assert "## Scope Rules" in prompt
        assert "## Files You May Change" in prompt
        assert prompt.endswith("## Hook Output\nfix.py:1:89: E501 Line too long")
        assert "## MUST_FIX Findings" not in prompt
        assert "## Approved Plan" not in prompt
        assert "--no-verify" in prompt


class TestHookRepairTurn:
    def _loop(
        self,
        make_git_repo: Callable[..., Path],
        name: str,
        hook: str,
        behaviors: list[object],
        docs: list[str] | None = None,
        *,
        manifest: dict[str, str] | None = None,
        plan: str | None = None,
        growth_budget_lines: int = DEFAULT_GROWTH_BUDGET_LINES,
    ) -> tuple[AutoDevResult, Path, _FixLoopRunner]:
        worktree = _worktree(make_git_repo, name, manifest=manifest)
        if plan is not None:
            worktree = _with_plan(worktree, plan)
        _install_pre_commit_hook(worktree, hook)
        runner = _FixLoopRunner(
            docs or [_MF_DOC, _CLEAN_DOC],
            fix_behaviors=behaviors,  # type: ignore[arg-type]
        )
        out, _ = _run_loop(
            runner,
            worktree,
            session_id=f"s-{name}",
            growth_budget_lines=growth_budget_lines,
        )
        return out, worktree, runner

    def test_lint_failure_gets_exactly_one_repair_turn_and_commits_and_pushes(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commit_argvs: list[list[str]] = []
        real_run_git = commit_module.run_git

        def _spy(argv: list[str], **kwargs: object) -> object:
            if argv and argv[0] == "commit":
                commit_argvs.append(list(argv))
            return real_run_git(argv, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(commit_module, "run_git", _spy)
        out, worktree, runner = self._loop(
            make_git_repo,
            "wt-repair-ok",
            _LINT_HOOK,
            [_MARKER_FIX, _CLEAN_REPAIR],
        )

        assert out.status != "blocked", out
        assert runner.fix_calls == 2
        fix_calls = [c for c in runner.calls if "workspace-write" in c["argv"]]  # type: ignore[operator]
        repair_prompt = str(fix_calls[1]["stdin"])
        assert "E501" in repair_prompt
        assert "## Scope Rules" in repair_prompt
        assert "## MUST_FIX Findings" not in repair_prompt
        head, _count, origin_tip = _heads(worktree)
        assert origin_tip == head
        assert commit_argvs
        assert all(
            "--no-verify" not in argv and "-n" not in argv for argv in commit_argvs
        )

    def test_repair_that_still_fails_the_hook_parks_hook_failed_with_repair_noted(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, worktree, runner = self._loop(
            make_git_repo,
            "wt-repair-rejected",
            _ALWAYS_FAILING_HOOK,
            [_MARKER_FIX, _CLEAN_REPAIR],
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert out.blocker is not None
        assert "commit was rejected again" in out.blocker.details
        assert out.blocker.recovery_hint is not None
        assert LEFT_STAGED_HINT in out.blocker.recovery_hint
        assert runner.fix_calls == 2
        assert "fix.py" in git_in(worktree, "diff", "--cached", "--name-only")

    def test_repair_touching_sensitive_out_of_scope_path_parks_scope_violation(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, _worktree_path, runner = self._loop(
            make_git_repo,
            "wt-repair-scope",
            _LINT_HOOK,
            [
                _MARKER_FIX,
                _editor("pyproject.toml", '[project]\nname = "z"\n'),
            ],
            manifest=_SENSITIVE,
        )

        assert out.status == "blocked"
        assert _blocker_reason(out) != CODEX_FIX_HOOK_FAILED
        assert runner.fix_calls == 2

    def test_repair_exceeding_growth_budget_parks_growth_budget(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, _worktree_path, runner = self._loop(
            make_git_repo,
            "wt-repair-growth",
            _LINT_HOOK,
            [_MARKER_FIX, _GROWTH_REPAIR],
            plan=_GROWTH_PLAN,
            growth_budget_lines=5,
        )

        assert _blocker_reason(out) == CODEX_FIX_GROWTH_BUDGET
        assert runner.fix_calls == 2
        assert out.blocker is not None
        assert "budget of 5" in out.blocker.details

    def test_missing_hook_binary_parks_without_repair(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, _worktree_path, runner = self._loop(
            make_git_repo,
            "wt-repair-nobin",
            "#!/bin/sh\nexec /nonexistent/bin/ruff check fix.py\n",
            [_MARKER_FIX, _CLEAN_REPAIR],
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert out.blocker is not None
        assert "Repair turn: not attempted." in out.blocker.details
        assert runner.fix_calls == 1

    def test_pre_commit_executable_not_found_parks_without_repair(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        hook = (
            "#!/bin/sh\necho '- hook id: ruff'\necho '- exit code: 1'\n"
            "echo 'Executable `ruff` not found'\necho 'fix.py'\nexit 1\n"
        )
        out, _worktree_path, runner = self._loop(
            make_git_repo, "wt-repair-exe", hook, [_MARKER_FIX, _CLEAN_REPAIR]
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert runner.fix_calls == 1

    def test_repair_turn_timeout_parks_turn_failed_and_restages(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        def _untracked_then_timeout(worktree: Path, _argv: list[str]) -> CodexRunResult:
            (worktree / "stray.py").write_text("x = 1\n", encoding="utf-8")
            return CodexRunResult(returncode=1, stdout="", stderr="", timed_out=True)

        out, worktree, runner = self._loop(
            make_git_repo,
            "wt-repair-timeout",
            _LINT_HOOK,
            [_MARKER_FIX, _untracked_then_timeout],
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert out.blocker is not None
        assert "the fix agent's repair turn failed" in out.blocker.details
        assert runner.fix_calls == 2
        assert "??" not in git_in(worktree, "status", "--porcelain")

    def test_failed_restage_after_repair_timeout_parks_as_git_error(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken_restage(_worktree: Path, _artifacts: frozenset[str]) -> None:
            raise subprocess.CalledProcessError(128, ["git", "add", "-A"])

        monkeypatch.setattr(commit_module, "_restage", _broken_restage)
        out, _worktree_path, runner = self._loop(
            make_git_repo,
            "wt-repair-restage",
            _LINT_HOOK,
            [
                _MARKER_FIX,
                CodexRunResult(returncode=1, stdout="", stderr="", timed_out=True),
            ],
        )

        assert _blocker_reason(out) == "codex_error"
        assert runner.fix_calls == 2

    def test_repair_nonzero_exit_parks_turn_failed(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, _worktree_path, runner = self._loop(
            make_git_repo,
            "wt-repair-exit",
            _LINT_HOOK,
            [
                _MARKER_FIX,
                CodexRunResult(returncode=2, stdout="", stderr="boom"),
            ],
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert out.blocker is not None
        assert "the fix agent's repair turn failed" in out.blocker.details
        assert runner.fix_calls == 2

    def test_repair_that_reverts_everything_is_tolerated_as_noop(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        def _revert(worktree: Path, _argv: list[str]) -> CodexRunResult:
            (worktree / "fix.py").unlink()
            return CodexRunResult(returncode=0, stdout="", stderr="")

        out, _worktree_path, runner = self._loop(
            make_git_repo,
            "wt-repair-revert",
            _LINT_HOOK,
            [_MARKER_FIX, _revert],
        )

        assert out.status != "blocked"
        assert runner.fix_calls == 2

    def test_repair_that_commits_on_its_own_parks_staged_set_mismatch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        def _self_commit(worktree: Path, _argv: list[str]) -> CodexRunResult:
            (worktree / "fix.py").write_text("patched = 1\n", encoding="utf-8")
            git_in(worktree, "add", "-A")
            git_in(worktree, "commit", "-m", "self commit")
            return CodexRunResult(returncode=0, stdout="", stderr="")

        out, _worktree_path, runner = self._loop(
            make_git_repo,
            "wt-repair-selfcommit",
            _LINT_HOOK,
            [_MARKER_FIX, _self_commit],
        )

        assert out.status == "blocked"
        assert runner.fix_calls == 2

    def test_secret_scanner_failure_never_reaches_the_agent(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        hook = (
            "#!/bin/sh\necho '- hook id: gitleaks'\necho '- exit code: 1'\n"
            "echo 'fix.py leaked SECRET-XYZ-999'\nexit 1\n"
        )
        out, _worktree_path, runner = self._loop(
            make_git_repo, "wt-repair-secret", hook, [_MARKER_FIX, _CLEAN_REPAIR]
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert runner.fix_calls == 1
        for call in runner.calls:
            assert "SECRET-XYZ-999" not in str(call.get("stdin") or "")

    def test_commit_msg_hook_failure_parks_without_repair(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-repair-msg")
        hook_path = worktree / ".git" / "hooks" / "commit-msg"
        hook_path.write_text(f"#!/bin/sh\necho '{_LINT_LINE}'\nexit 1\n")
        hook_path.chmod(0o755)
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_MARKER_FIX])

        out, _ = _run_loop(runner, worktree, session_id="s-repair-msg")

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert runner.fix_calls == 1

    def test_at_most_one_repair_per_cycle(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, _worktree_path, runner = self._loop(
            make_git_repo,
            "wt-repair-once",
            _ALWAYS_FAILING_HOOK,
            [_MARKER_FIX, _CLEAN_REPAIR, _CLEAN_REPAIR],
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert runner.fix_calls == 2
