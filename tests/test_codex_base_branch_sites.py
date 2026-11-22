"""RFC 0020 S3b (#3840): codex-lane crossings that carry the row's base.

Each crossing is pinned twice where it can differ: a based row (the base
differs from the client default) and a null row (the base is the default, so
the text or value must be byte-identical to before S3b).
"""

from __future__ import annotations

import ast
from pathlib import Path

from cw.codex_impl._run import _merge_note

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_merge_note_names_the_row_base_for_a_based_row() -> None:
    note = _merge_note(("src/a.py",), "epic/x")
    assert note is not None
    assert note.startswith("origin/epic/x conflicts with this branch in:")
    assert "- src/a.py" in note


def test_merge_note_keeps_the_default_branch_text_for_a_null_row() -> None:
    note = _merge_note(("src/a.py",), None)
    assert note is not None
    assert note.startswith("origin's default branch conflicts with this branch in:")


def test_merge_note_is_none_without_conflicts() -> None:
    assert _merge_note((), "epic/x") is None


def test_codex_plan_has_no_default_branch_read() -> None:
    """The codex plan lane has no merge-target read; the S3b claim is vacuous there."""
    plan_dir = _REPO_ROOT / "src" / "cw" / "codex_plan"
    reads = [
        f"{path.name}:{node.lineno}"
        for path in plan_dir.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "default_branch"
    ]
    assert reads == []
