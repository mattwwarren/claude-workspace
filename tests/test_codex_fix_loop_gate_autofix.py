"""Unit tests for the fix loop's declared-autofix helpers (#3412).

Pins :func:`declared_autofixes` (the exception-safe config reader),
:func:`changed_paths` (tree-to-tree path set) and :func:`revert_paths` (the
path-targeted restore the gate runner uses to undo a rejected autofix).
"""

from __future__ import annotations

import logging
import re
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop import gate_autofix
from cw.codex_fix_loop.baseline import staged_cycle_tree
from cw.codex_fix_loop.gate_autofix import (
    changed_paths,
    declared_autofixes,
    ignored_on_disk,
    revert_paths,
    revert_to_tree,
)
from cw.review_findings import REVIEW_VERDICT_JSON_RELATIVE_PATH
from tests._shared.git_repos import (
    commit_tracked_file,
    git_in,
    replace_dir_with_file,
    write_non_utf8_named_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_LOGGER = "cw.codex_fix_loop.gate_autofix"
_CONTEXT = "ticket=T-1 session=s-1 cycle=1 gate=fmt"
_ARTIFACT = str(REVIEW_VERDICT_JSON_RELATIVE_PATH)
_ARTIFACTS = frozenset({_ARTIFACT})
_EXEC_MODE = 0o755
_IGNORE_RULES = "keep.log\nnode_modules/\n"
# revert_to_tree's staged_cycle_tree calls: current, targeted verify, fallback verify.
_FALLBACK_VERIFY_CALL = 3


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

    def test_non_utf8_named_tracked_file_is_restored(self, repo: Path) -> None:
        """#3419: a ``-z`` read of a non-UTF-8 name no longer raises."""
        name = write_non_utf8_named_file(repo, "a\n")
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "add non-utf8 name")
        before = _tree(repo)
        (repo / name).write_text("b\n", encoding="utf-8")
        after = _tree(repo)

        revert_paths(repo, before, changed_paths(repo, before, after))

        assert (repo / name).read_text(encoding="utf-8") == "a\n"
        assert _tree(repo) == before

    def test_non_utf8_named_added_file_is_removed(self, repo: Path) -> None:
        before = _tree(repo)
        name = write_non_utf8_named_file(repo)
        after = _tree(repo)

        revert_paths(repo, before, changed_paths(repo, before, after))

        assert not (repo / name).exists()
        assert _tree(repo) == before

    def test_a_preserved_added_path_stays_on_disk_but_leaves_the_index(
        self, repo: Path
    ) -> None:
        """#3420: a preserved path (or one under a preserved dir) is not unlinked."""
        before = _tree(repo)
        (repo / "keep.log").write_text("keep\n", encoding="utf-8")
        (repo / "cache").mkdir()
        (repo / "cache" / "x").write_text("cached\n", encoding="utf-8")
        (repo / "gone.py").write_text("g = 1\n", encoding="utf-8")
        after = _tree(repo)

        revert_paths(
            repo,
            before,
            changed_paths(repo, before, after),
            preserve=frozenset({"keep.log", "cache/"}),
        )

        assert (repo / "keep.log").read_text(encoding="utf-8") == "keep\n"
        assert (repo / "cache" / "x").read_text(encoding="utf-8") == "cached\n"
        assert not (repo / "gone.py").exists()
        assert git_in(repo, "ls-files", "--", "keep.log", "cache") == ""


class TestChangedPathsDiffFilter:
    def test_deleted_only(self, repo: Path) -> None:
        """#3427: ``diff_filter='D'`` reports only deletions."""
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")
        (repo / "new.py").write_text("n = 1\n", encoding="utf-8")
        (repo / "other.py").unlink()
        after = _tree(repo)

        assert changed_paths(repo, before, after, diff_filter="D") == frozenset(
            {"other.py"}
        )

    def test_a_rename_reports_the_old_path_as_deleted(self, repo: Path) -> None:
        before = _tree(repo)
        (repo / "tracked.py").rename(repo / "renamed.py")
        after = _tree(repo)

        assert changed_paths(repo, before, after, diff_filter="D") == frozenset(
            {"tracked.py"}
        )

    def test_modify_and_add_report_nothing(self, repo: Path) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")
        (repo / "new.py").write_text("n = 1\n", encoding="utf-8")
        after = _tree(repo)

        assert changed_paths(repo, before, after, diff_filter="D") == frozenset()


def _commit_ignore_rules(repo: Path) -> None:
    (repo / ".gitignore").write_text(_IGNORE_RULES, encoding="utf-8")
    git_in(repo, "add", ".gitignore")
    git_in(repo, "commit", "-m", "ignore rules")


def _write_ignored_files(repo: Path) -> None:
    (repo / "keep.log").write_text("secret\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x").write_text("dep\n", encoding="utf-8")


class TestIgnoredOnDisk:
    def test_lists_ignored_files_and_collapses_ignored_dirs(self, repo: Path) -> None:
        _commit_ignore_rules(repo)
        _write_ignored_files(repo)
        (repo / "untracked.py").write_text("u = 1\n", encoding="utf-8")

        assert ignored_on_disk(repo) == frozenset({"keep.log", "node_modules/"})

    def test_nothing_ignored_is_empty(self, repo: Path) -> None:
        assert ignored_on_disk(repo) == frozenset()


def _fallback_outcomes(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        m.group(1)
        for r in caplog.records
        if (m := re.search(r"revert_fallback .*outcome=(\w+)", r.getMessage()))
    ]


def _incomplete_logged(caplog: pytest.LogCaptureFixture) -> bool:
    return any(
        "codex_fix_gate_autofix_revert_incomplete" in r.getMessage()
        and _CONTEXT in r.getMessage()
        and r.levelno == logging.WARNING
        for r in caplog.records
    )


def _dir_to_file(repo: Path, artifacts: frozenset[str] = frozenset()) -> str:
    """Commit ``pkg/mod.py``, snapshot, then replace ``pkg`` with a file.

    The targeted revert raises on this shape: ``git rm --cached pkg`` refuses
    a directory without ``-r`` once ``restore`` has recreated ``pkg/mod.py``.
    """
    commit_tracked_file(repo, "pkg/mod.py", "m = 1\n")
    before = staged_cycle_tree(repo, artifacts)
    replace_dir_with_file(repo)
    return before


def _write_artifact(repo: Path, content: str) -> Path:
    path = repo / _ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _git_output_failing_on(
    monkeypatch: pytest.MonkeyPatch, predicate: Callable[[Sequence[str]], bool]
) -> None:
    real = gate_autofix.git_output

    def _wrapper(
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
        errors: str = "strict",
    ) -> str:
        if predicate(argv):
            raise subprocess.CalledProcessError(128, list(argv), "", "boom")
        return real(argv, cwd=cwd, timeout=timeout, errors=errors)

    monkeypatch.setattr(gate_autofix, "git_output", _wrapper)


class TestRevertToTree:
    def test_a_clean_targeted_revert_logs_nothing(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            revert_to_tree(repo, frozenset(), before, context=_CONTEXT)

        assert _tree(repo) == before
        assert not caplog.records

    def test_an_ignore_rule_rewrite_keeps_a_previously_ignored_file(
        self, repo: Path
    ) -> None:
        """#3420: emptying ``.gitignore`` makes ``keep.log`` look added."""
        (repo / ".gitignore").write_text("keep.log\n", encoding="utf-8")
        git_in(repo, "add", ".gitignore")
        git_in(repo, "commit", "-m", "ignore keep.log")
        (repo / "keep.log").write_text("secret\n", encoding="utf-8")
        before = _tree(repo)
        preserve = ignored_on_disk(repo)
        (repo / ".gitignore").write_text("", encoding="utf-8")

        revert_to_tree(repo, frozenset(), before, preserve=preserve, context=_CONTEXT)

        assert (repo / "keep.log").read_text(encoding="utf-8") == "secret\n"
        assert (repo / ".gitignore").read_text(encoding="utf-8") == "keep.log\n"
        assert _tree(repo) == before

    def test_a_raising_targeted_revert_falls_back_to_read_tree(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#3423: the dir-to-file shape makes ``revert_paths`` raise."""
        before = _dir_to_file(repo)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            revert_to_tree(repo, frozenset(), before, context=_CONTEXT)

        assert (repo / "pkg" / "mod.py").read_text(encoding="utf-8") == "m = 1\n"
        assert _tree(repo) == before
        assert git_in(repo, "status", "--porcelain") == ""
        assert _incomplete_logged(caplog)
        assert _fallback_outcomes(caplog) == ["restored"]

    def test_an_incomplete_targeted_revert_falls_back(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")
        monkeypatch.setattr(gate_autofix, "revert_paths", lambda *_a, **_k: None)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            revert_to_tree(repo, frozenset(), before, context=_CONTEXT)

        assert (repo / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
        assert _tree(repo) == before
        assert _incomplete_logged(caplog)
        assert _fallback_outcomes(caplog) == ["restored"]

    def test_a_failing_read_tree_raises_and_keeps_the_artifact(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        artifact = _write_artifact(repo, "b")
        before = _dir_to_file(repo, _ARTIFACTS)
        _git_output_failing_on(monkeypatch, lambda argv: argv[0] == "read-tree")

        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER),
            pytest.raises(subprocess.CalledProcessError),
        ):
            revert_to_tree(repo, _ARTIFACTS, before, context=_CONTEXT)

        assert artifact.read_text(encoding="utf-8") == "b"
        assert _fallback_outcomes(caplog) == ["failed"]

    def test_a_tree_still_wrong_after_the_fallback_raises(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")
        monkeypatch.setattr(gate_autofix, "revert_paths", lambda *_a, **_k: None)
        real = gate_autofix.staged_cycle_tree
        calls: list[int] = []

        def _third_is_wrong(worktree: Path, artifacts: frozenset[str]) -> str:
            calls.append(1)
            tree = real(worktree, artifacts)
            return "0" * len(tree) if len(calls) == _FALLBACK_VERIFY_CALL else tree

        monkeypatch.setattr(gate_autofix, "staged_cycle_tree", _third_is_wrong)

        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER),
            pytest.raises(subprocess.CalledProcessError, match="read-tree"),
        ):
            revert_to_tree(repo, frozenset(), before, context=_CONTEXT)

        assert _fallback_outcomes(caplog) == ["failed"]

    def test_a_failing_preserve_drop_logs_failed_once_and_raises(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        before = _tree(repo)
        (repo / "tracked.py").write_text("x   =   1\n", encoding="utf-8")
        monkeypatch.setattr(gate_autofix, "revert_paths", lambda *_a, **_k: None)
        _git_output_failing_on(monkeypatch, lambda argv: "rm" in argv)

        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER),
            pytest.raises(subprocess.CalledProcessError),
        ):
            revert_to_tree(
                repo,
                frozenset(),
                before,
                preserve=frozenset({"keep.log"}),
                context=_CONTEXT,
            )

        assert _fallback_outcomes(caplog) == ["failed"]

    def test_a_failing_artifact_rewrite_never_masks_the_git_error(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_artifact(repo, "b")
        before = _dir_to_file(repo, _ARTIFACTS)
        _git_output_failing_on(monkeypatch, lambda argv: argv[0] == "read-tree")

        def _no_write(*_a: object, **_k: object) -> int:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(Path, "write_bytes", _no_write)

        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER),
            pytest.raises(subprocess.CalledProcessError),
        ):
            revert_to_tree(repo, _ARTIFACTS, before, context=_CONTEXT)

        assert any("artifact_rewrite_failed" in r.getMessage() for r in caplog.records)

    def test_a_failing_artifact_rewrite_still_returns_after_a_good_fallback(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        commit_tracked_file(repo, _ARTIFACT, "a")
        _write_artifact(repo, "b")
        before = _dir_to_file(repo, _ARTIFACTS)

        def _no_write(*_a: object, **_k: object) -> int:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(Path, "write_bytes", _no_write)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            revert_to_tree(repo, _ARTIFACTS, before, context=_CONTEXT)

        assert _fallback_outcomes(caplog) == ["restored"]
        assert any("artifact_rewrite_failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("variant", ["tracked-modified", "untracked"])
    def test_the_fallback_keeps_a_review_artifact(
        self, repo: Path, variant: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The MUST_FIX: ``read-tree --reset -u`` would reset a tracked artifact."""
        if variant == "tracked-modified":
            commit_tracked_file(repo, _ARTIFACT, "a")
        artifact = _write_artifact(repo, "b")
        artifact.chmod(_EXEC_MODE)
        before = _dir_to_file(repo, _ARTIFACTS)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            revert_to_tree(repo, _ARTIFACTS, before, context=_CONTEXT)

        assert _fallback_outcomes(caplog) == ["restored"]
        assert staged_cycle_tree(repo, _ARTIFACTS) == before
        assert artifact.read_text(encoding="utf-8") == "b"
        assert stat.S_IMODE(artifact.stat().st_mode) == _EXEC_MODE

    def test_the_fallback_keeps_preserved_ignored_files(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Without the preserve drop, ``read-tree -u`` deletes staged ignored files."""
        _commit_ignore_rules(repo)
        _write_ignored_files(repo)
        commit_tracked_file(repo, "pkg/mod.py", "m = 1\n")
        before = _tree(repo)
        preserve = ignored_on_disk(repo)
        (repo / ".gitignore").write_text("", encoding="utf-8")
        replace_dir_with_file(repo)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            revert_to_tree(
                repo, frozenset(), before, preserve=preserve, context=_CONTEXT
            )

        assert _fallback_outcomes(caplog) == ["restored"]
        assert (repo / "keep.log").read_text(encoding="utf-8") == "secret\n"
        assert (repo / "node_modules" / "x").read_text(encoding="utf-8") == "dep\n"
        assert (repo / ".gitignore").read_text(encoding="utf-8") == _IGNORE_RULES
        assert _tree(repo) == before
