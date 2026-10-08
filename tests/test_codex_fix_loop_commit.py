"""Tests for cw.codex_fix_loop.commit — one cycle's guarded fix-and-commit (#2633).

The cycle is measured against its own clean start: a dirty worktree refuses
the cycle before codex runs, and the commit carries only the measured paths.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop import _build_fix_prompt, _commit_fix_cycle, commit
from cw.codex_fix_loop.baseline import CycleBaseline, DirtyStart
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, StagedSetMismatchError
from cw.codex_review import (
    CODEX_FIX_DIRTY_START,
    CODEX_FIX_HOOK_FAILED,
    CODEX_FIX_SCOPE_DRIFT,
    CODEX_FIX_SCOPE_VIOLATION,
    CODEX_TIMEOUT,
)
from cw.codex_runner import CodexRunResult
from cw.executor_diagnostics import diagnostics_bundle_dir
from tests._codex_review_helpers import (
    _install_pre_commit_hook,
    _measured_from_head,
    _seed_conflicting_cherry_pick,
    _stage_merge_from_other_branch,
    _write,
)
from tests.conftest import _make_finding, git_in
from tests.test_codex_fix_loop import (
    _CLEAN_DOC,
    _MANIFEST,
    _MF_DOC,
    _PYPROJECT_CONTENT,
    _editor,
    _FixLoopRunner,
    _run_loop,
    _with_plan,
    _worktree,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult

_SENSITIVE = {
    ".claude/sensitive-files.yml": _MANIFEST,
    "pyproject.toml": _PYPROJECT_CONTENT,
}


def _blocker_reason(out: AutoDevResult) -> str:
    assert out.status == "blocked"
    assert out.blocker is not None
    return out.blocker.reason


def _heads(worktree: Path) -> tuple[str, str, str]:
    """HEAD, the branch's commit count, and the pushed origin tip."""
    return (
        git_in(worktree, "rev-parse", "HEAD"),
        git_in(worktree, "rev-list", "--count", "HEAD"),
        git_in(worktree, "rev-parse", "origin/feature"),
    )


class TestDirtyStartRefusal:
    def test_merge_head_parks_before_codex_runs(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """Evidence 3 repro: a staged merge is never read as a cycle edit."""
        worktree = _worktree(make_git_repo, "wt-dirty-merge", manifest=_SENSITIVE)
        _stage_merge_from_other_branch(
            worktree, {"pyproject.toml": '[project]\nname = "main"\n'}
        )
        head = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor("new.py")])

        out, _ = _run_loop(runner, worktree, session_id="s-dirty-merge")

        assert _blocker_reason(out) == CODEX_FIX_DIRTY_START
        assert runner.fix_calls == 0
        assert git_in(worktree, "rev-parse", "HEAD") == head
        assert git_in(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        assert "pyproject.toml" in git_in(worktree, "diff", "--cached", "--name-only")

    def test_prestaged_squash_pyproject_parks_without_commit_or_push(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-dirty-squash", manifest=_SENSITIVE)
        _stage_merge_from_other_branch(
            worktree, {"pyproject.toml": '[project]\nname = "main"\n'}, squash=True
        )
        before = _heads(worktree)
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor("new.py")])

        out, _ = _run_loop(runner, worktree, session_id="s-dirty-squash")

        assert _blocker_reason(out) == CODEX_FIX_DIRTY_START
        assert runner.fix_calls == 0
        assert _heads(worktree) == before
        assert "M  pyproject.toml" in git_in(worktree, "status", "--porcelain")

    def test_unmerged_index_without_merge_head_parks_as_dirty_start(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-dirty-pick")
        _seed_conflicting_cherry_pick(worktree)
        head = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-dirty-pick")

        assert _blocker_reason(out) == CODEX_FIX_DIRTY_START
        assert runner.fix_calls == 0
        assert git_in(worktree, "rev-parse", "HEAD") == head
        assert len(git_in(worktree, "ls-files", "-u").splitlines()) == 3
        assert "<<<<<<<" in (worktree / "pyproject.toml").read_text()

    def test_unstaged_edit_and_untracked_file_park_as_dirty_start(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-dirty-hand")
        _write(worktree / "new.py", "edited by hand\n")
        _write(worktree / "stray.py", "x = 1\n")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-dirty-hand")

        assert _blocker_reason(out) == CODEX_FIX_DIRTY_START
        assert out.blocker is not None
        assert "stray.py" in out.blocker.details
        assert runner.fix_calls == 0


class TestBeginCycle:
    def test_baseline_probe_git_failure_parks_codex_error(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-begin-fail")

        def _boom(_worktree: Path) -> CycleBaseline | DirtyStart:
            raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

        monkeypatch.setattr(commit, "capture_cycle_baseline", _boom)
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-begin-fail")

        assert _blocker_reason(out) == "codex_error"
        assert runner.fix_calls == 0


class TestCleanStartInvariant:
    def test_fix_commit_contains_only_the_cycles_changes(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-invariant")
        seen: list[CycleBaseline | DirtyStart] = []
        real = commit.capture_cycle_baseline

        def _spy(path: Path) -> CycleBaseline | DirtyStart:
            seen.append(real(path))
            return seen[-1]

        monkeypatch.setattr(commit, "capture_cycle_baseline", _spy)
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-invariant")

        assert out.status != "blocked"
        sha = git_in(worktree, "rev-parse", "HEAD")
        changed = git_in(
            worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", sha
        )
        assert changed.split() == ["fix.py"]
        assert isinstance(seen[0], CycleBaseline)
        assert seen[0].tree_sha == git_in(worktree, "rev-parse", f"{sha}~1^{{tree}}")


class TestStagedSetGuard:
    def test_extra_staged_path_parks_uncommitted(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-staged-extra")
        head = git_in(worktree, "rev-parse", "HEAD")
        real = commit._cycle_breach

        def _racing(*args: object, **kwargs: object) -> object:
            result = real(*args, **kwargs)
            _write(worktree / "race.py", "r = 1\n")
            git_in(worktree, "add", "race.py")
            return result

        monkeypatch.setattr(commit, "_cycle_breach", _racing)
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-staged-extra")

        assert _blocker_reason(out) == CODEX_FIX_SCOPE_DRIFT
        assert out.blocker is not None
        assert "Staged but not measured:\n- race.py" in out.blocker.details
        assert git_in(worktree, "rev-parse", "HEAD") == head
        assert git_in(worktree, "rev-parse", "origin/feature") == head
        assert "race.py" in git_in(worktree, "diff", "--cached", "--name-only")

    def test_fix_invocation_that_commits_itself_parks_uncommitted(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-staged-selfcommit")
        start_head = git_in(worktree, "rev-parse", "HEAD")

        def _self_commit(path: Path, _argv: list[str]) -> CodexRunResult:
            _write(path / "fix.py", "patched = 1\n")
            git_in(path, "add", "fix.py")
            git_in(path, "commit", "-m", "codex committed on its own")
            return CodexRunResult(returncode=0, stdout="", stderr="")

        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_self_commit])

        out, _ = _run_loop(runner, worktree, session_id="s-staged-selfcommit")

        assert _blocker_reason(out) == CODEX_FIX_SCOPE_DRIFT
        assert out.blocker is not None
        assert out.blocker.recovery_hint is not None
        assert f"git log {start_head}..HEAD" in out.blocker.recovery_hint
        assert git_in(worktree, "rev-parse", "origin/feature") == start_head

    def test_editor_that_writes_nothing_is_a_tolerated_no_op(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-staged-noop")

        assert (
            _commit_fix_cycle(
                worktree,
                cycle=1,
                findings=[_make_finding()],
                measured_paths=frozenset(),
            )
            is None
        )


class TestScopeViolationUsesBaseline:
    def test_cycle_edit_of_sensitive_path_still_parks(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-sv-baseline", manifest=_SENSITIVE)
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[_editor("pyproject.toml", '[project]\nname = "z"\n')],
        )

        out, _ = _run_loop(runner, worktree, session_id="s-sv-baseline")

        assert _blocker_reason(out) == CODEX_FIX_SCOPE_VIOLATION
        assert out.blocker is not None
        assert out.blocker.recovery_hint is not None
        assert LEFT_STAGED_HINT in out.blocker.recovery_hint


class TestCycleBreachOrder:
    def test_scope_violation_is_checked_before_fence(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _with_plan(
            _worktree(make_git_repo, "wt-breach-order", manifest=_SENSITIVE)
        )
        runner = _FixLoopRunner(
            [_MF_DOC],
            fix_behaviors=[_editor("pyproject.toml", '[project]\nname = "z"\n')],
        )

        out, _ = _run_loop(runner, worktree, session_id="s-breach-order")

        assert _blocker_reason(out) == CODEX_FIX_SCOPE_VIOLATION


class TestInvocationFailure:
    """Advisory soundness RISK 1: a failed invocation that left edits behind."""

    @staticmethod
    def _timeout_after(
        write: bool,
    ) -> Callable[[Path, list[str]], CodexRunResult]:
        def _behave(path: Path, _argv: list[str]) -> CodexRunResult:
            if write:
                _write(path / "half.py", "half = 1\n")
            return CodexRunResult(returncode=1, stdout="", stderr="", timed_out=True)

        return _behave

    def test_timeout_with_partial_edits_is_not_retry_eligible(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-timeout-dirty")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[self._timeout_after(True)])

        out, _ = _run_loop(runner, worktree, session_id="s-timeout-dirty")

        assert _blocker_reason(out) == CODEX_TIMEOUT
        assert out.blocker is not None
        assert out.blocker.retry_eligible is None
        assert "left uncommitted edits" in out.blocker.details
        assert CODEX_FIX_DIRTY_START in out.blocker.details

    def test_timeout_with_clean_tree_stays_retry_eligible(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-timeout-clean")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[self._timeout_after(False)])

        out, _ = _run_loop(runner, worktree, session_id="s-timeout-clean")

        assert _blocker_reason(out) == CODEX_TIMEOUT
        assert out.blocker is not None
        assert out.blocker.retry_eligible is True
        assert "left uncommitted edits" not in out.blocker.details

    def test_unmeasurable_tree_keeps_the_transient_park(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-timeout-probe-fail")
        real = commit.capture_cycle_baseline
        calls: list[Path] = []

        def _second_call_fails(path: Path) -> CycleBaseline | DirtyStart:
            calls.append(path)
            if len(calls) > 1:
                raise subprocess.CalledProcessError(128, ["git", "status"])
            return real(path)

        monkeypatch.setattr(commit, "capture_cycle_baseline", _second_call_fails)
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[self._timeout_after(True)])

        out, _ = _run_loop(runner, worktree, session_id="s-timeout-probe-fail")

        assert _blocker_reason(out) == CODEX_TIMEOUT
        assert out.blocker is not None
        assert out.blocker.retry_eligible is True


class TestCommitFixCycleGuard:
    def test_extra_staged_path_raises_and_leaves_head(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-guard-direct")
        head = git_in(worktree, "rev-parse", "HEAD")
        _write(worktree / "fix.py", "patched = 1\n")
        git_in(worktree, "add", "fix.py")

        with pytest.raises(StagedSetMismatchError) as excinfo:
            _commit_fix_cycle(
                worktree,
                cycle=1,
                findings=[_make_finding()],
                measured_paths=frozenset(),
            )

        assert excinfo.value.staged == frozenset({"fix.py"})
        assert git_in(worktree, "rev-parse", "HEAD") == head

    def test_clean_tree_with_measured_paths_raises(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-guard-clean")

        with pytest.raises(StagedSetMismatchError) as excinfo:
            _commit_fix_cycle(
                worktree,
                cycle=1,
                findings=[_make_finding()],
                measured_paths=frozenset({"fix.py"}),
            )

        assert excinfo.value.staged == frozenset()

    def test_measured_paths_matching_the_staged_set_commit(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-guard-match")
        _write(worktree / "fix.py", "patched = 1\n")

        sha = _commit_fix_cycle(
            worktree,
            cycle=1,
            findings=[_make_finding()],
            measured_paths=_measured_from_head(worktree),
        )

        assert sha == git_in(worktree, "rev-parse", "HEAD")


_ONCE_FAILING_HOOK = (
    "#!/bin/sh\n"
    'counter="$(git rev-parse --git-dir)/pre-commit-calls"\n'
    'if [ -f "$counter" ]; then exit 0; fi\n'
    'echo 1 > "$counter"\n'
    "exit 1\n"
)


class TestHookFailure:
    def _run(
        self, make_git_repo: Callable[..., Path], name: str, hook: str
    ) -> tuple[AutoDevResult, Path, str, _FixLoopRunner]:
        worktree = _worktree(make_git_repo, name)
        _install_pre_commit_hook(worktree, hook)
        head = git_in(worktree, "rev-parse", "HEAD")
        runner = _FixLoopRunner([_MF_DOC, _CLEAN_DOC], fix_behaviors=[_editor()])
        out, _ = _run_loop(runner, worktree, session_id=f"s-{name}")
        return out, worktree, head, runner

    def test_persistent_hook_failure_parks_as_hook_failed(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, worktree, head, runner = self._run(
            make_git_repo, "wt-hook-lint", "#!/bin/sh\necho LINT-ERROR-XYZ\nexit 1\n"
        )

        assert _blocker_reason(out) == CODEX_FIX_HOOK_FAILED
        assert out.blocker is not None
        assert "LINT-ERROR-XYZ" in out.blocker.details
        assert out.blocker.recovery_hint is not None
        assert LEFT_STAGED_HINT in out.blocker.recovery_hint
        assert runner.fix_calls == 1
        assert git_in(worktree, "rev-parse", "HEAD") == head
        assert "fix.py" in git_in(worktree, "diff", "--cached", "--name-only")

    def test_secret_scanner_hook_failure_does_not_post_output(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, *_ = self._run(
            make_git_repo,
            "wt-hook-scanner",
            "#!/bin/sh\necho '- hook id: gitleaks'\necho '- exit code: 1'\n"
            "echo 'leaked SECRET-ABC123'\nexit 1\n",
        )

        assert out.blocker is not None
        assert out.blocker.recovery_hint is not None
        assert "SECRET-ABC123" not in out.blocker.details
        assert "SECRET-ABC123" not in out.blocker.recovery_hint
        assert "gitleaks" in out.blocker.details
        assert "exit code 1" in out.blocker.details

    def test_hook_rewrite_retry_still_succeeds(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        out, worktree, head, _ = self._run(
            make_git_repo, "wt-hook-once", _ONCE_FAILING_HOOK
        )

        assert out.status != "blocked"
        assert git_in(worktree, "rev-parse", "HEAD~1") == head

    def test_non_hook_commit_failure_stays_codex_error(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-hook-noident")
        # An empty committer name makes `git commit` itself fail, no hook.
        git_in(worktree, "config", "user.name", "")
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-hook-noident")

        assert _blocker_reason(out) == "codex_error"
        bundle = diagnostics_bundle_dir("s-hook-noident")
        [failure] = bundle.glob("fix-cycle-1-runtime_error-*.json")
        # The commit's stderr is captured now, not lost to the driver log.
        assert "empty ident name" in failure.read_text(encoding="utf-8")

    def test_push_failure_stays_codex_error(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        worktree = _worktree(make_git_repo, "wt-hook-push")
        _install_pre_commit_hook(worktree, "#!/bin/sh\nexit 0\n")
        git_in(worktree, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        runner = _FixLoopRunner([_MF_DOC], fix_behaviors=[_editor()])

        out, _ = _run_loop(runner, worktree, session_id="s-hook-push")

        assert _blocker_reason(out) == "codex_error"


class TestFixPromptConstraints:
    _FINDINGS = (_make_finding(file="new.py", summary="MFA"),)

    def _prompt(self, **extra: str | None) -> str:
        return _build_fix_prompt(
            list(self._FINDINGS),
            plan_text="plan body",
            ticket_text="ticket body",
            cycle=1,
            allowed_files=frozenset({"new.py"}),
            **extra,
        )

    def test_default_prompt_is_byte_identical(self) -> None:
        default = self._prompt()

        assert default == self._prompt(constraints_section=None)
        assert "Binding Operator Constraints" not in default

    def test_constraints_section_rendered_after_scope_rules_before_fence(
        self,
    ) -> None:
        section = "## Binding Operator Constraints\nDo not add `foo_bar`."

        prompt = self._prompt(constraints_section=section)

        assert prompt.replace(f"{section}\n\n", "") == self._prompt()
        scope = prompt.index("## Scope Rules")
        constraints = prompt.index("## Binding Operator Constraints")
        fence = prompt.index("## Files You May Change")
        assert scope < constraints < fence
