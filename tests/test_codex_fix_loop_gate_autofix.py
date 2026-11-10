"""Unit tests for the fix loop's declared-autofix helpers (#3412).

Pins :func:`declared_autofixes` (the exception-safe config reader),
:func:`changed_paths` (tree-to-tree path set) and :func:`revert_paths` (the
path-targeted restore the gate runner uses to undo a rejected autofix).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop import gate_autofix
from cw.codex_fix_loop.baseline import staged_cycle_tree
from cw.codex_fix_loop.gate_autofix import (
    changed_paths,
    declared_autofixes,
    revert_paths,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    path = make_git_repo("autofix-wt")
    commit_tracked_file(path, "tracked.py", "x = 1\n")
    commit_tracked_file(path, "other.py", "y = 1\n")
    return path


def _write_config(repo: Path, text: str) -> None:
    config = repo / ".claude" / "project-config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")


def _tree(repo: Path) -> str:
    return staged_cycle_tree(repo, frozenset())


class TestDeclaredAutofixes:
    def test_no_file_is_empty(self, repo: Path) -> None:
        assert declared_autofixes(repo) == {}

    @pytest.mark.parametrize(
        "text",
        [
            "sprint_buildout:\n  a: b\n",
            "quality_gates: nope\n",
            "quality_gates:\n  timeouts:\n    pre-commit: 600\n",
            "quality_gates:\n  autofix: nope\n",
            "quality_gates:\n  autofix:\n    - a\n",
            "- just\n- a list\n",
            "quality_gates: [unclosed\n",
        ],
    )
    def test_malformed_or_absent_block_is_empty(self, repo: Path, text: str) -> None:
        _write_config(repo, text)

        assert declared_autofixes(repo) == {}

    def test_keeps_valid_entries_only_and_strips(self, repo: Path) -> None:
        _write_config(
            repo,
            "quality_gates:\n"
            "  autofix:\n"
            "    ruff-format: '  uv run ruff format .  '\n"
            "    blank: '   '\n"
            "    number: 3\n"
            "    nothing:\n"
            "    listy: [a, b]\n"
            "    7: seven\n",
        )

        assert declared_autofixes(repo) == {"ruff-format": "uv run ruff format ."}

    def test_exact_name_only_never_a_prefix(self, repo: Path) -> None:
        _write_config(repo, "quality_gates:\n  autofix:\n    ruff-format: fix\n")

        declared = declared_autofixes(repo)

        assert declared.get("ruff-format") == "fix"
        assert declared.get("ruff-format-2") is None
        assert declared.get("ruff") is None

    def test_non_utf8_config_returns_empty(self, repo: Path) -> None:
        config = repo / ".claude" / "project-config.yaml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"\xff\xfe\x00quality_gates:\n  autofix:\n    a: b\n")

        assert declared_autofixes(repo) == {}

    def test_oserror_returns_empty(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            msg = "unreadable"
            raise PermissionError(msg)

        monkeypatch.setattr(gate_autofix, "load_project_config_dict", _boom)

        assert declared_autofixes(repo) == {}


class TestChangedPaths:
    def test_modified_added_and_deleted(self, repo: Path) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")
        (repo / "new.py").write_text("n = 1\n", encoding="utf-8")
        (repo / "other.py").unlink()

        after = _tree(repo)

        assert changed_paths(repo, before, after) == frozenset(
            {"tracked.py", "new.py", "other.py"}
        )
        assert changed_paths(repo, before, before) == frozenset()

    def test_a_rename_reports_both_sides(self, repo: Path) -> None:
        before = _tree(repo)
        (repo / "tracked.py").rename(repo / "renamed.py")

        after = _tree(repo)

        assert changed_paths(repo, before, after) == frozenset(
            {"tracked.py", "renamed.py"}
        )

    def test_non_ascii_name_is_real_by_default_and_c_quoted_on_request(
        self, repo: Path
    ) -> None:
        commit_tracked_file(repo, "café.py", "z = 1\n")
        before = _tree(repo)
        (repo / "café.py").write_text("z   =   1\n", encoding="utf-8")

        after = _tree(repo)

        assert changed_paths(repo, before, after) == frozenset({"café.py"})
        quoted = changed_paths(repo, before, after, c_quoted=True)
        assert quoted == frozenset({'"caf\\303\\251.py"'})


class TestRevertPaths:
    def test_restores_modified_deletes_added_and_restores_deleted(
        self, repo: Path
    ) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")
        (repo / "new.py").write_text("n = 1\n", encoding="utf-8")
        (repo / "other.py").unlink()
        after = _tree(repo)

        revert_paths(repo, before, changed_paths(repo, before, after))

        assert (repo / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
        assert not (repo / "new.py").exists()
        assert (repo / "other.py").read_text(encoding="utf-8") == "y = 1\n"
        assert _tree(repo) == before

    def test_leaves_paths_outside_the_set_alone(self, repo: Path) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")
        (repo / "other.py").write_text("y   =   1\n", encoding="utf-8")
        _tree(repo)

        revert_paths(repo, before, frozenset({"tracked.py"}))

        assert (repo / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
        assert (repo / "other.py").read_text(encoding="utf-8") == "y   =   1\n"

    def test_empty_path_set_is_a_no_op(self, repo: Path) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")

        revert_paths(repo, before, frozenset())

        assert (repo / "tracked.py").read_text(encoding="utf-8") == "x   =   1\n"

    def test_path_beginning_with_a_colon_is_literal(self, repo: Path) -> None:
        (repo / ":col.py").write_text("c = 1\n", encoding="utf-8")
        git_in(repo, "--literal-pathspecs", "add", "--", ":col.py")
        git_in(repo, "commit", "-m", "add colon file")
        before = _tree(repo)
        (repo / ":col.py").write_text("c   =   1\n", encoding="utf-8")
        after = _tree(repo)

        revert_paths(repo, before, changed_paths(repo, before, after))

        assert (repo / ":col.py").read_text(encoding="utf-8") == "c = 1\n"
        assert _tree(repo) == before

    def test_nested_paths_are_restored_and_removed(self, repo: Path) -> None:
        (repo / "pkg").mkdir()
        commit_tracked_file(repo, "pkg/mod.py", "m = 1\n")
        before = _tree(repo)
        (repo / "pkg" / "mod.py").write_text("m   =   1\n", encoding="utf-8")
        (repo / "pkg" / "added.py").write_text("a = 1\n", encoding="utf-8")
        after = _tree(repo)

        revert_paths(repo, before, changed_paths(repo, before, after))

        assert (repo / "pkg" / "mod.py").read_text(encoding="utf-8") == "m = 1\n"
        assert not (repo / "pkg" / "added.py").exists()
        assert _tree(repo) == before

    def test_handles_non_ascii_filename(self, repo: Path) -> None:
        commit_tracked_file(repo, "café.py", "z = 1\n")
        before = _tree(repo)
        (repo / "café.py").write_text("z   =   1\n", encoding="utf-8")
        after = _tree(repo)

        revert_paths(repo, before, changed_paths(repo, before, after))

        assert (repo / "café.py").read_text(encoding="utf-8") == "z = 1\n"
        assert _tree(repo) == before

    def test_long_path_lists_are_chunked(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gate_autofix, "_CHUNK", 2)
        before = _tree(repo)
        names = [f"n{i}.py" for i in range(5)]
        for name in names:
            (repo / name).write_text("n = 1\n", encoding="utf-8")
        after = _tree(repo)

        revert_paths(repo, before, changed_paths(repo, before, after))

        assert not [name for name in names if (repo / name).exists()]
        assert _tree(repo) == before
