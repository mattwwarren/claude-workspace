"""Tests for src/cw/review_strategy.py — resolve_review_strategy unit tests.

Mirrors tests/test_tracker.py 1:1 in shape: a helper writes a
``.claude/project-config.yaml`` under a tmp root, and each test asserts the
default-safe resolution behaviour (any failure / absent key -> ci fallback).
"""

from __future__ import annotations

from pathlib import Path

from cw.review_strategy import (
    PROJECT_CONFIG_RELPATH,
    ReviewStrategy,
    resolve_review_strategy,
)


def _write_config(root: Path, content: str) -> None:
    cfg_dir = root / ".claude"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "project-config.yaml").write_text(content, encoding="utf-8")


class TestResolveReviewStrategy:
    def test_missing_file_returns_ci_default(self, tmp_path: Path) -> None:
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("ci", None)

    def test_malformed_yaml_returns_ci_default(self, tmp_path: Path) -> None:
        _write_config(tmp_path, ": invalid yaml {{{")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("ci", None)

    def test_non_dict_root_returns_ci_default(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "just-a-string\n")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("ci", None)

    def test_non_dict_review_strategy_returns_ci_default(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "review_strategy: not-a-dict\n")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("ci", None)

    def test_absent_key_returns_ci_default(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary:\n    system: github-issues\n")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("ci", None)

    def test_mode_ci_returns_no_handle(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "review_strategy:\n  mode: ci\n")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("ci", None)

    def test_mode_repo_owner_returns_handle(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path, "review_strategy:\n  mode: repo_owner\n  repo_owner: alice\n"
        )
        assert resolve_review_strategy(tmp_path) == ReviewStrategy(
            "repo_owner", "alice"
        )

    def test_mode_reviewer_team_returns_handle(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "review_strategy:\n  mode: reviewer_team\n  reviewer_team: org/core\n",
        )
        assert resolve_review_strategy(tmp_path) == ReviewStrategy(
            "reviewer_team", "org/core"
        )

    def test_repo_owner_mode_missing_handle_returns_none_handle(
        self, tmp_path: Path
    ) -> None:
        # Runtime never wedges: a misconfigured mode still resolves (handle None);
        # the act phase turns the None handle into a fail-safe correction, and
        # `cw doctor` surfaces the typo — resolve_review_strategy stays lenient.
        _write_config(tmp_path, "review_strategy:\n  mode: repo_owner\n")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("repo_owner", None)

    def test_reviewer_team_mode_missing_handle_returns_none_handle(
        self, tmp_path: Path
    ) -> None:
        _write_config(tmp_path, "review_strategy:\n  mode: reviewer_team\n")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy(
            "reviewer_team", None
        )

    def test_unrecognized_mode_returns_ci_default(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "review_strategy:\n  mode: bogus\n  repo_owner: a\n")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("ci", None)

    def test_non_string_mode_returns_ci_default(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "review_strategy:\n  mode: 42\n")
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("ci", None)

    def test_non_string_handle_returns_none_handle(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path, "review_strategy:\n  mode: repo_owner\n  repo_owner: 42\n"
        )
        assert resolve_review_strategy(tmp_path) == ReviewStrategy("repo_owner", None)


class TestProjectConfigRelpath:
    def test_relpath_matches_expected(self) -> None:
        assert Path(".claude") / "project-config.yaml" == PROJECT_CONFIG_RELPATH
