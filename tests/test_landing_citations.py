"""Tests for .claude/scripts/landing_citations.py (RFC 0020 B2, #3784).

Loaded via importlib, mirroring ``tests/test_classify_merge_conflict.py``:
the script lives outside the ``src/`` tree. Every test calls in process,
because ``.claude/scripts`` coverage counts in-process calls only.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "landing_citations.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("landing_citations", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("landing_citations", mod)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


class TestRenderCitations:
    def test_issue_numbers_are_cited_one_line_each(self) -> None:
        result = _mod.render_citations(["12", "7"])
        assert result.lines == ("Closes #12", "Closes #7")
        assert result.uncitable == ()

    @pytest.mark.parametrize("child", ["#12", " 12 ", "\t#12\n"])
    def test_ids_are_normalized(self, child: str) -> None:
        assert _mod.render_citations([child]).lines == ("Closes #12",)

    def test_duplicates_are_cited_once(self) -> None:
        result = _mod.render_citations(["12", "#12", "13", "12"])
        assert result.lines == ("Closes #12", "Closes #13")

    def test_uncitable_ids_are_returned_in_input_order(self) -> None:
        result = _mod.render_citations(["ABC-12", "12x", "0", ""])
        assert result.lines == ()
        assert result.uncitable == ("ABC-12", "12x", "0", "")

    def test_a_mixed_list_keeps_both_groups_in_input_order(self) -> None:
        result = _mod.render_citations(["ABC-1", "12", "ABC-2", "3"])
        assert result.lines == ("Closes #12", "Closes #3")
        assert result.uncitable == ("ABC-1", "ABC-2")


class TestMain:
    def test_children_flag_prints_the_block_and_reports_uncitable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _mod.main(["--children", "12,13 ABC-1"]) == 0
        out, err = capsys.readouterr()
        assert out == "Closes #12\nCloses #13\n"
        assert err == "uncitable: ABC-1\n"

    def test_stdin_is_read_without_the_flag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _mod.main([], stdin=io.StringIO("12\n13\n")) == 0
        out, err = capsys.readouterr()
        assert out == "Closes #12\nCloses #13\n"
        assert err == ""

    def test_only_uncitable_ids_still_exit_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _mod.main(["--children", "ABC-1"]) == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert err == "uncitable: ABC-1\n"

    @pytest.mark.parametrize(
        ("argv", "stdin"),
        [(["--children", ""], None), ([], " \n,\n")],
        ids=["empty-flag", "empty-stdin"],
    )
    def test_no_ids_exits_one_with_nothing_on_stdout(
        self,
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
        stdin: str | None,
    ) -> None:
        stream = io.StringIO(stdin) if stdin is not None else None
        assert _mod.main(argv, stdin=stream) == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert "no child ids supplied" in err

    def test_default_stdin_is_sys_stdin(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("5"))
        assert _mod.main([]) == 0
        assert capsys.readouterr().out == "Closes #5\n"


class TestIsolation:
    _TREE = ast.parse(_SCRIPT.read_text(encoding="utf-8"))

    def test_imports_nothing_from_cw(self) -> None:
        names: list[str] = []
        for node in ast.walk(self._TREE):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names.append(node.module)
        assert names
        assert not [n for n in names if n == "cw" or n.startswith("cw.")]

    def test_launches_nothing(self) -> None:
        source = _SCRIPT.read_text(encoding="utf-8")
        for token in ("subprocess", "os.system", "os.exec", "popen"):
            assert token not in source
        names = {node.id for node in ast.walk(self._TREE) if isinstance(node, ast.Name)}
        assert "os" not in names
        constants = {
            node.value
            for node in ast.walk(self._TREE)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not constants & {"git", "gh"}
