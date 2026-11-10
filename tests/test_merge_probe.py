"""Tests for ``cw.merge_probe`` (#2979): the read-only default-merge probe."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw import merge_probe
from cw.merge_probe import (
    PROBE_TIMEOUT_SECONDS,
    merge_tree,
    probe_default_merge,
)
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


def _commit(repo: Path, files: Mapping[str, str], message: str) -> str:
    """Write *files* under *repo*, commit them, and return the new HEAD sha."""
    for relpath, content in files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git_in(repo, "add", "--", relpath)
    git_in(repo, "commit", "-m", message)
    return git_in(repo, "rev-parse", "HEAD")


def _diverged(
    make_git_repo: Callable[..., Path],
    *,
    base: Mapping[str, str],
    branch: Mapping[str, str],
    main: Mapping[str, str],
) -> Path:
    """A repo on ``feature`` whose ``origin/main`` moved on after the fork.

    ``origin/main`` is a local tracking ref set with ``update-ref`` so no
    remote is needed: the probe reads the ref, never fetches it.
    """
    repo = make_git_repo("repo")
    _commit(repo, base, "base")
    git_in(repo, "checkout", "-b", "feature")
    _commit(repo, branch, "branch change")
    git_in(repo, "checkout", "main")
    _commit(repo, main, "main change")
    git_in(repo, "update-ref", "refs/remotes/origin/main", "main")
    git_in(repo, "checkout", "feature")
    return repo


def _snapshot(repo: Path) -> tuple[str, str]:
    return git_in(repo, "status", "--porcelain"), git_in(repo, "ls-files", "-s")


class TestProbeStates:
    def test_up_to_date_when_main_is_already_merged(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )
        git_in(repo, "merge", "--no-edit", "origin/main")

        probe = probe_default_merge(repo, "main")

        assert probe.state == "up_to_date"
        assert probe.base_sha == git_in(repo, "rev-parse", "origin/main")
        assert probe.conflicted_files == ()

    def test_clean_for_a_disjoint_change(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )

        assert probe_default_merge(repo, "main").state == "clean"

    def test_clean_for_an_overlapping_textually_clean_change(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"shared.txt": "one\ntwo\nthree\nfour\nfive\n"},
            branch={"shared.txt": "ONE\ntwo\nthree\nfour\nfive\n"},
            main={"shared.txt": "one\ntwo\nthree\nfour\nFIVE\n"},
        )

        assert probe_default_merge(repo, "main").state == "clean"

    def test_conflict_names_sorted_files_including_odd_paths(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        odd = "sp ace é.txt"
        repo = _diverged(
            make_git_repo,
            base={"z.txt": "base\n", odd: "base\n", "ok.txt": "ok\n"},
            branch={"z.txt": "branch\n", odd: "branch\n"},
            main={"z.txt": "main\n", odd: "main\n", "ok.txt": "OK\n"},
        )
        before = _snapshot(repo)

        probe = probe_default_merge(repo, "main")

        assert probe.state == "conflict"
        assert probe.conflicted_files == (odd, "z.txt")
        assert _snapshot(repo) == before
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_probe_leaves_index_and_worktree_untouched_on_clean(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )
        before = _snapshot(repo)
        head = git_in(repo, "rev-parse", "HEAD")

        probe_default_merge(repo, "main")

        assert _snapshot(repo) == before
        assert git_in(repo, "rev-parse", "HEAD") == head

    def test_union_driver_changelog_is_clean(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """The spike: ``merge-tree`` honours ``CHANGELOG.md merge=union``."""
        repo = _diverged(
            make_git_repo,
            base={
                ".gitattributes": "CHANGELOG.md merge=union\n",
                "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n",
            },
            branch={"CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n- branch\n"},
            main={"CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n- main\n"},
        )

        assert probe_default_merge(repo, "main").state == "clean"
        git_in(repo, "merge", "--no-edit", "origin/main")
        assert "- branch" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")


class TestProbeUnavailable:
    def test_unresolvable_base(self, make_git_repo: Callable[..., Path]) -> None:
        repo = make_git_repo("repo")

        probe = probe_default_merge(repo, "main")

        assert probe.state == "unavailable"
        assert probe.base_sha is None
        assert "origin/main does not resolve" in probe.detail

    def test_oserror(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo("repo")

        def _raise(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            msg = "no git"
            raise OSError(msg)

        monkeypatch.setattr(merge_probe, "run_git", _raise)

        probe = probe_default_merge(repo, "main")

        assert probe.state == "unavailable"
        assert "could not run" in probe.detail

    def test_timeout_and_every_call_is_bounded(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )
        real = merge_probe.run_git
        timeouts: list[object] = []

        def _record(
            argv: list[str],
            *,
            cwd: Path | None = None,
            timeout: float | None = None,
            **_rest: object,
        ) -> subprocess.CompletedProcess[str]:
            timeouts.append(timeout)
            if argv[0] == "merge-tree":
                raise subprocess.TimeoutExpired(["git", *argv], 10.0)
            return real(argv, cwd=cwd, capture_output=True, timeout=timeout)

        monkeypatch.setattr(merge_probe, "run_git", _record)

        probe = probe_default_merge(repo, "main")

        assert probe.state == "unavailable"
        assert "timed out" in probe.detail
        assert probe.base_sha is not None
        assert timeouts == [PROBE_TIMEOUT_SECONDS] * 3

    @pytest.mark.parametrize("returncode", [2, 128, 129])
    def test_merge_tree_error_exit(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
    ) -> None:
        """rc>=2, including git < 2.38 rejecting ``--write-tree`` (129)."""
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )
        real = merge_probe.run_git

        def _fake(
            argv: list[str],
            *,
            cwd: Path | None = None,
            timeout: float | None = None,
            **_rest: object,
        ) -> subprocess.CompletedProcess[str]:
            if argv[0] == "merge-tree":
                return subprocess.CompletedProcess(
                    ["git", *argv], returncode, "", "error: unknown option\n"
                )
            return real(argv, cwd=cwd, capture_output=True, timeout=timeout)

        monkeypatch.setattr(merge_probe, "run_git", _fake)

        probe = probe_default_merge(repo, "main")

        assert probe.state == "unavailable"
        assert f"exited {returncode}: error: unknown option" in probe.detail

    def test_merge_base_error_exit(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )
        real = merge_probe.run_git

        def _fake(
            argv: list[str],
            *,
            cwd: Path | None = None,
            timeout: float | None = None,
            **_rest: object,
        ) -> subprocess.CompletedProcess[str]:
            if argv[0] == "merge-base":
                return subprocess.CompletedProcess(["git", *argv], 128, "", "")
            return real(argv, cwd=cwd, capture_output=True, timeout=timeout)

        monkeypatch.setattr(merge_probe, "run_git", _fake)

        probe = probe_default_merge(repo, "main")

        assert probe.state == "unavailable"
        assert "merge-base exited 128: no output" in probe.detail


class TestMergeTree:
    def test_clean_returns_the_tree_oid_without_trailing_nul(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )
        git_in(repo, "merge", "--no-edit", "origin/main")

        found = merge_tree(repo, "HEAD^1", "HEAD^2")

        assert found.status == "clean"
        assert found.tree == git_in(repo, "rev-parse", "HEAD^{tree}")

    def test_conflict_returns_the_conflict_list(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"s.txt": "base\n"},
            branch={"s.txt": "branch\n"},
            main={"s.txt": "main\n"},
        )

        found = merge_tree(repo, "HEAD", "origin/main")

        assert found.status == "conflict"
        assert found.conflicted_files == ("s.txt",)
        assert found.tree is not None

    def test_timeout_is_unavailable(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo("repo")

        def _timeout(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(["git"], 1.0)

        monkeypatch.setattr(merge_probe, "run_git", _timeout)

        found = merge_tree(repo, "HEAD", "HEAD", timeout=1.0)

        assert found.status == "unavailable"
        assert found.detail == "git merge-tree timed out after 1s"

    def test_unreadable_output_is_unavailable(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo("repo")

        def _garbage(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(["git", *argv], 0, "not-an-oid\n", "")

        monkeypatch.setattr(merge_probe, "run_git", _garbage)

        assert merge_tree(repo, "HEAD", "HEAD").status == "unavailable"


class TestFalseConflictIsNotReproduced:
    """S5: a merge git would not write itself is never reproduced (fail-closed)."""

    def test_hand_edited_clean_merge_tree_differs(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )
        git_in(repo, "merge", "--no-commit", "--no-ff", "origin/main")
        (repo / "a.txt").write_text("hand edit\n", encoding="utf-8")
        git_in(repo, "add", "a.txt")
        git_in(repo, "commit", "--no-edit")

        found = merge_tree(repo, "HEAD^1", "HEAD^2")

        assert found.status == "clean"
        assert found.tree != git_in(repo, "rev-parse", "HEAD^{tree}")

    def test_strategy_option_merge_over_a_real_conflict_is_not_reproduced(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"s.txt": "base\n"},
            branch={"s.txt": "branch\n"},
            main={"s.txt": "main\n"},
        )
        git_in(repo, "merge", "--no-edit", "-X", "ours", "origin/main")

        found = merge_tree(repo, "HEAD^1", "HEAD^2")

        assert found.status == "conflict"


class TestPublicHelpers:
    """``resolve_base``, ``git_bounded`` and ``GitFailedError`` are public (#3190)."""

    def test_resolve_base_returns_the_sha(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _diverged(
            make_git_repo,
            base={"a.txt": "a\n"},
            branch={"b.txt": "b\n"},
            main={"c.txt": "c\n"},
        )

        sha = merge_probe.resolve_base(repo, "main", PROBE_TIMEOUT_SECONDS)

        assert sha == git_in(repo, "rev-parse", "origin/main")

    def test_resolve_base_raises_when_absent(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("repo")

        with pytest.raises(merge_probe.GitFailedError, match="does not resolve"):
            merge_probe.resolve_base(repo, "main", PROBE_TIMEOUT_SECONDS)

    def test_git_bounded_maps_a_timeout(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo("repo")

        def _timeout(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            cmd = "git status"
            raise subprocess.TimeoutExpired(cmd, 2.0)

        monkeypatch.setattr(merge_probe, "run_git", _timeout)

        with pytest.raises(merge_probe.GitFailedError, match="timed out after 2s"):
            merge_probe.git_bounded(repo, ["status"], 2.0)

    def test_git_bounded_maps_an_oserror(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_git_repo("repo")

        def _raise(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            msg = "no git"
            raise OSError(msg)

        monkeypatch.setattr(merge_probe, "run_git", _raise)

        with pytest.raises(merge_probe.GitFailedError, match="could not run: no git"):
            merge_probe.git_bounded(repo, ["status"], 2.0)

    def test_docstring_keeps_the_leaf_rule_and_names_the_script(self) -> None:
        doc = " ".join((merge_probe.__doc__ or "").split())

        assert "The module is a leaf: it imports only :mod:`cw._git`." in doc
        assert "``.claude/scripts/check_changelog_frozen.py``" in doc
        assert "public for ``cw.changelog_sync``" in doc

    def test_the_old_private_names_are_gone(self) -> None:
        for name in ("_GitFailedError", "_resolve_base", "_git"):
            assert not hasattr(merge_probe, name), name
