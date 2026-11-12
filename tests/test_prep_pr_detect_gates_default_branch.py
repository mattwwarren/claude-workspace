"""``detect_gates`` reads gates from the remote default branch's CLAUDE.md.

Gates are repo policy: with no explicit path they come from the committed
CLAUDE.md on ``origin/HEAD``, so a branch cut before a gate change, or one that
edits its own gate list, is still held to the default branch's gates. When that
copy is unavailable the worktree's CLAUDE.md is used, as before.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests._shared.git_repos import git_in
from tests.test_prep_pr_state import _mod

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_MAIN_BLOCK = "## Quality Gates\n\n```bash\nmain-only-check .\n```\n"


def _commit_default_branch(repo: Path, relpath: str, content: str) -> None:
    """Commit *relpath* and point ``origin/HEAD`` at it, without a real remote."""
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git_in(repo, "add", relpath)
    git_in(repo, "commit", "-m", f"add {relpath}")
    git_in(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git_in(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def _commands(result: dict[str, Any], name: str | None = None) -> list[str]:
    return [g["command"] for g in result["gates"] if name is None or g["name"] == name]


def _detect() -> dict[str, Any]:
    return cast("dict[str, Any]", _mod.detect_gates())


class TestDefaultBranchWins:
    def test_a_stale_worktree_copy_loses_to_the_default_branch(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("repo")
        _commit_default_branch(repo, "CLAUDE.md", _MAIN_BLOCK)
        (repo / "CLAUDE.md").write_text("# old branch copy, no gates\n")
        monkeypatch.chdir(repo)
        result = _detect()
        assert result["claude_md_source"] == "default-branch"
        assert result["detected_from"] == ["CLAUDE.md"]
        assert _commands(result) == ["main-only-check ."]

    def test_a_branch_cannot_weaken_its_own_gates(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("repo")
        _commit_default_branch(
            repo, "CLAUDE.md", "## Quality Gates\n- lint: strict-lint\n"
        )
        (repo / "CLAUDE.md").write_text("## Quality Gates\n- lint: true\n")
        monkeypatch.chdir(repo)
        assert _commands(_detect(), "lint") == ["strict-lint"]

    def test_the_default_branch_copy_wins_even_when_it_declares_no_gates(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("repo")
        _commit_default_branch(repo, "CLAUDE.md", "")
        (repo / "CLAUDE.md").write_text("## Quality Gates\n- lint: local-lint\n")
        monkeypatch.chdir(repo)
        result = _detect()
        assert result["claude_md_source"] == "default-branch"
        assert _commands(result, "lint") == []

    def test_a_subdirectory_reads_its_own_claude_md_on_the_default_branch(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("repo")
        (repo / "CLAUDE.md").write_text("## Quality Gates\n- lint: root-lint\n")
        _commit_default_branch(
            repo, "pkg/CLAUDE.md", "## Quality Gates\n- lint: pkg-lint\n"
        )
        monkeypatch.chdir(repo / "pkg")
        assert _commands(_detect(), "lint") == ["pkg-lint"]


class TestFallsBackToWorktree:
    def test_without_origin_head(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("repo")
        (repo / "CLAUDE.md").write_text("## Quality Gates\n- lint: local-lint\n")
        monkeypatch.chdir(repo)
        result = _detect()
        assert result["claude_md_source"] == "worktree"
        assert _commands(result, "lint") == ["local-lint"]

    def test_when_the_file_is_not_on_the_default_branch(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = make_git_repo("repo")
        _commit_default_branch(repo, "CLAUDE.md", _MAIN_BLOCK)
        nested = repo / "nested"
        nested.mkdir()
        (nested / "CLAUDE.md").write_text("## Quality Gates\n- lint: nested-lint\n")
        monkeypatch.chdir(nested)
        with caplog.at_level(logging.WARNING):
            result = _detect()
        assert result["claude_md_source"] == "worktree"
        assert _commands(result, "lint") == ["nested-lint"]
        assert caplog.records == []

    def test_an_empty_default_branch_ref_never_reads_the_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``git show ":./CLAUDE.md"`` (no ref) reads the *index* copy, which
        # would silently stand in for the default branch.
        calls: list[str] = []

        def empty_ref(args: list[str], **_kwargs: object) -> str:
            calls.append(args[0])
            return ""

        monkeypatch.setattr(_mod, "_run_git", empty_ref)
        assert _mod._default_branch_claude_md() is None
        assert calls == ["symbolic-ref"]

    def test_outside_a_git_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "CLAUDE.md").write_text("## Quality Gates\n- lint: local-lint\n")
        monkeypatch.chdir(tmp_path)
        result = _detect()
        assert result["claude_md_source"] == "worktree"
        assert _commands(result, "lint") == ["local-lint"]

    def test_the_fallback_is_quiet(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = make_git_repo("repo")
        (repo / "CLAUDE.md").write_text("## Quality Gates\n- lint: local-lint\n")
        monkeypatch.chdir(repo)
        with caplog.at_level(logging.WARNING):
            _detect()
        assert caplog.records == []

    def test_no_git_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        def missing(*_args: object, **_kwargs: object) -> object:
            raise FileNotFoundError

        monkeypatch.setattr(_mod.subprocess, "run", missing)
        assert _mod._default_branch_claude_md() is None


class TestExplicitPath:
    def test_an_explicit_path_is_never_overridden(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("repo")
        _commit_default_branch(
            repo, "CLAUDE.md", "## Quality Gates\n- lint: main-lint\n"
        )
        custom = repo / "custom.md"
        custom.write_text("## Quality Gates\n- lint: custom-lint\n")
        monkeypatch.chdir(repo)
        result = _mod.detect_gates(claude_md_path=custom)
        assert result["claude_md_source"] == "worktree"
        assert _commands(result, "lint") == ["custom-lint"]


class TestRunGitQuiet:
    def _fail(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        def boom(*_args: object, **_kwargs: object) -> object:
            raise error

        monkeypatch.setattr(_mod.subprocess, "run", boom)

    @pytest.mark.parametrize(
        "error",
        [subprocess.CalledProcessError(1, "git", stderr="nope"), FileNotFoundError()],
        ids=["git-error", "git-missing"],
    )
    def test_quiet_failure_returns_default_without_logging(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        self._fail(monkeypatch, error)
        with caplog.at_level(logging.WARNING):
            assert _mod._run_git(["status"], default="d", quiet=True) == "d"
        assert caplog.records == []

    @pytest.mark.parametrize(
        "error",
        [subprocess.CalledProcessError(1, "git", stderr="nope"), FileNotFoundError()],
        ids=["git-error", "git-missing"],
    )
    def test_default_failure_still_logs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        self._fail(monkeypatch, error)
        with caplog.at_level(logging.WARNING):
            assert _mod._run_git(["status"], default="d") == "d"
        assert caplog.records
