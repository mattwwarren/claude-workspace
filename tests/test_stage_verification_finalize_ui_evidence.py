"""Tests for cw.stage_verification.finalize_ui_evidence (#3543).

``find_ui_files`` runs real git in a sandboxed repo; ``check_ui_evidence`` is a
pure function over its result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.stage_verification.finalize_ui_evidence import (
    UiFiles,
    check_ui_evidence,
    find_ui_files,
)
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_UI = "frontends/app/X.tsx"
_NO_MEDIA = "## Summary\n\nChanged the widget.\n"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> tuple[Path, str]:
    """A repo and its fork point (the empty base commit)."""
    worktree = make_git_repo("ui-evidence")
    return worktree, git_in(worktree, "rev-parse", "HEAD")


def _config(worktree: Path, text: str) -> None:
    path = worktree / ".claude" / "project-config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _files(worktree: Path, fork: str) -> UiFiles:
    return find_ui_files(
        worktree, fork_point=fork, head_sha=git_in(worktree, "rev-parse", "HEAD")
    )


class TestFindUiFiles:
    def test_no_ui_file_in_the_diff_is_empty_not_unreadable(
        self, repo: tuple[Path, str]
    ) -> None:
        worktree, fork = repo
        commit_tracked_file(worktree, "src/app.py")
        files = _files(worktree, fork)
        assert files == UiFiles()

    def test_a_frontend_file_is_listed(self, repo: tuple[Path, str]) -> None:
        worktree, fork = repo
        commit_tracked_file(worktree, _UI)
        assert _files(worktree, fork).paths == (_UI,)

    @pytest.mark.parametrize(
        "path",
        [
            "frontends/app/__tests__/X.tsx",
            "frontends/app/X.test.tsx",
            "frontends/app/X.spec.ts",
        ],
    )
    def test_test_files_are_ignored(self, repo: tuple[Path, str], path: str) -> None:
        worktree, fork = repo
        commit_tracked_file(worktree, path)
        assert _files(worktree, fork).paths == ()

    def test_an_empty_pattern_is_a_no_op(self, repo: tuple[Path, str]) -> None:
        worktree, fork = repo
        _config(worktree, "ui_paths_regex: ^$\n")
        commit_tracked_file(worktree, _UI)
        assert _files(worktree, fork) == UiFiles()

    def test_a_custom_pattern_matches_its_own_paths(
        self, repo: tuple[Path, str]
    ) -> None:
        worktree, fork = repo
        _config(worktree, "ui_paths_regex: ^web/.*\\.vue$\n")
        commit_tracked_file(worktree, "web/A.vue")
        commit_tracked_file(worktree, _UI)
        assert _files(worktree, fork).paths == ("web/A.vue",)

    @pytest.mark.parametrize("config", ["other: 1\n", "ui_paths_regex: 5\n", ": : :\n"])
    def test_an_absent_or_non_string_value_uses_the_default(
        self, repo: tuple[Path, str], config: str
    ) -> None:
        worktree, fork = repo
        _config(worktree, config)
        commit_tracked_file(worktree, _UI)
        files = _files(worktree, fork)
        assert files.paths == (_UI,)
        assert files.friction == ()

    def test_an_invalid_pattern_falls_back_with_friction(
        self, repo: tuple[Path, str]
    ) -> None:
        worktree, fork = repo
        _config(worktree, "ui_paths_regex: '('\n")
        commit_tracked_file(worktree, _UI)
        files = _files(worktree, fork)
        assert files.paths == (_UI,)
        assert files.friction == ("ui_paths_regex_invalid",)

    def test_an_empty_fork_point_is_unreadable(self, repo: tuple[Path, str]) -> None:
        worktree, _fork = repo
        files = _files(worktree, "")
        assert files.unreadable is True
        assert files.paths == ()

    def test_a_failing_diff_is_unreadable(self, repo: tuple[Path, str]) -> None:
        worktree, _fork = repo
        files = _files(worktree, "c" * 40)
        assert files.unreadable is True


class TestCheckUiEvidence:
    def test_an_unreadable_diff_holds_for_any_body(self) -> None:
        for body in (None, _NO_MEDIA, "<img src=x>"):
            verdict = check_ui_evidence(UiFiles(unreadable=True), body=body)
            assert verdict.hold is True
            assert verdict.friction == ("ui_evidence_unreadable",)

    @pytest.mark.parametrize("body", [None, _NO_MEDIA])
    def test_no_paths_is_no_hold_and_no_friction(self, body: str | None) -> None:
        verdict = check_ui_evidence(UiFiles(), body=body)
        assert (verdict.hold, verdict.friction) == (False, ())

    def test_paths_and_a_body_without_media_hold(self) -> None:
        verdict = check_ui_evidence(UiFiles(paths=(_UI,)), body=_NO_MEDIA)
        assert verdict.hold is True
        assert verdict.friction == ("ui_evidence_missing",)

    def test_paths_and_no_body_hold_as_unreadable(self) -> None:
        verdict = check_ui_evidence(UiFiles(paths=(_UI,)), body=None)
        assert verdict.hold is True
        assert verdict.friction == ("ui_evidence_unreadable",)

    @pytest.mark.parametrize(
        "media",
        [
            '<img src="a.png">',
            "![a](b)",
            "https://github.com/user-attachments/assets/1",
            "demo.webm",
            "demo.mp4",
        ],
    )
    def test_each_media_marker_clears_the_gate(self, media: str) -> None:
        verdict = check_ui_evidence(UiFiles(paths=(_UI,)), body=f"{_NO_MEDIA}\n{media}")
        assert (verdict.hold, verdict.friction) == (False, ())

    def test_the_files_friction_rides_every_verdict(self) -> None:
        carried = ("ui_paths_regex_invalid",)
        held = check_ui_evidence(
            UiFiles(paths=(_UI,), friction=carried), body=_NO_MEDIA
        )
        assert held.friction == (*carried, "ui_evidence_missing")
        clear = check_ui_evidence(UiFiles(friction=carried), body=None)
        assert clear.friction == carried
