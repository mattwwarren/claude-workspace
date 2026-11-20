"""Unit tests for the shared tolerated-path porcelain filter (#3693)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cw.models.client import ClientConfig
from cw.tolerated_paths import (
    CHECK_DIRTY_START,
    CHECK_POST_REVIEW_MUTATION,
    split_tolerated,
    tolerated_paths,
)

_TOLERATED = frozenset({".codex/config.toml"})


class TestSplitTolerated:
    def test_empty_set_returns_status_unchanged_by_identity(self) -> None:
        status = " M .codex/config.toml\n?? junk.txt\n"
        split = split_tolerated(status, frozenset())
        assert split.kept is status
        assert split.paths == ()

    @pytest.mark.parametrize("code", [" M", "M ", "MM", "??", " D", "A "])
    def test_exact_match_removed_for_each_status(self, code: str) -> None:
        status = f"{code} .codex/config.toml\n"
        split = split_tolerated(status, _TOLERATED)
        assert split.kept == ""
        assert split.paths == (".codex/config.toml",)

    def test_unlisted_line_is_kept(self) -> None:
        status = " M src/a.py\n"
        split = split_tolerated(status, _TOLERATED)
        assert split.kept == status
        assert split.paths == ()

    @pytest.mark.parametrize("path", [".codex/config.toml.bak", "x/.codex/config.toml"])
    def test_no_prefix_or_substring_match(self, path: str) -> None:
        status = f" M {path}\n"
        split = split_tolerated(status, _TOLERATED)
        assert split.kept == status
        assert split.paths == ()

    def test_rename_touching_tolerated_path_is_kept(self) -> None:
        status = "R  old.py -> .codex/config.toml\n"
        split = split_tolerated(status, _TOLERATED)
        assert split.kept == status
        assert split.paths == ()

    def test_tolerated_line_does_not_mask_other_path(self) -> None:
        status = " M .codex/config.toml\n M src/a.py\n"
        split = split_tolerated(status, _TOLERATED)
        assert split.kept == " M src/a.py\n"
        assert split.paths == (".codex/config.toml",)

    def test_duplicate_lines_collapse_in_porcelain_order(self) -> None:
        other = frozenset({"b.txt", "a.txt"})
        status = " M b.txt\n M a.txt\n?? b.txt\n"
        split = split_tolerated(status, other)
        assert split.kept == ""
        assert split.paths == ("b.txt", "a.txt")

    def test_check_logs_one_warning_per_path_without_content(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        status = " M .codex/config.toml\n M other.txt\n"
        with caplog.at_level(logging.WARNING, logger="cw.tolerated_paths"):
            split_tolerated(status, _TOLERATED, check=CHECK_DIRTY_START)
        records = [r for r in caplog.records if r.name == "cw.tolerated_paths"]
        assert len(records) == 1
        assert records[0].args == (".codex/config.toml", CHECK_DIRTY_START)

    def test_no_check_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="cw.tolerated_paths"):
            split_tolerated(" M .codex/config.toml\n", _TOLERATED)
        assert [r for r in caplog.records if r.name == "cw.tolerated_paths"] == []

    def test_post_review_check_name_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="cw.tolerated_paths"):
            split_tolerated(
                " M .codex/config.toml\n",
                _TOLERATED,
                check=CHECK_POST_REVIEW_MUTATION,
            )
        records = [r for r in caplog.records if r.name == "cw.tolerated_paths"]
        assert records[0].args == (".codex/config.toml", CHECK_POST_REVIEW_MUTATION)


class TestToleratedPathsResolver:
    def test_returns_frozenset_of_client_list(self) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=Path("/dev/null"),
            review={"tolerated_worktree_paths": [".codex/config.toml"]},
        )
        assert tolerated_paths(client) == frozenset({".codex/config.toml"})

    def test_empty_config_gives_empty_frozenset(self) -> None:
        client = ClientConfig(name="test", workspace_path=Path("/dev/null"))
        assert tolerated_paths(client) == frozenset()
