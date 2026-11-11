"""Nested-bullet resolution in the ``## Files Modified`` parsers (#2909).

A bare filename in a bullet nested under a directory bullet (``- some/dir/``
then ``  - tool_a.py``) resolves to ``some/dir/tool_a.py``. The rule lives in
two mirrored copies that cannot share an import:

- ``.claude/scripts/check_plan_scope_conformance.py::_parse_files_modified``
- ``cw.plan_files.parse_plan_files_modified``

This module pins the cross-tree parity contract: every case runs through both
parsers and must yield the same list. Hermetic: string literals only, no git,
subprocess, clock, or filesystem. Assertions are on path lists, never wording.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from pathlib import Path

import pytest

from cw import plan_files
from cw.plan_files import parse_plan_files_modified
from tests._shared.guard_fences import load_guard_script_module
from tests._shared.review_builders import _plan_text

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_plan_scope_conformance.py"

_mod = load_guard_script_module(_SCRIPT, "check_plan_scope_conformance")

_PARSERS = [
    pytest.param(_mod._parse_files_modified, id="script"),
    pytest.param(parse_plan_files_modified, id="package"),
]


def _plan(body: str) -> str:
    """Wrap *body* in a ``## Files Modified`` section with a trailing section."""
    return f"# Plan\n\n## Files Modified\n\n{body}\n\n## Ambiguities\n\nNONE\n"


_THIRTEEN = [f"src/cw/tools/tool_{n}.py" for n in range(13)]

_CASES: list[tuple[str, str, list[str]]] = [
    (
        "dir_with_nested_bare_files",
        _plan("- some/dir/\n  - tool_a.py\n  - tool_b.py"),
        ["some/dir/", "some/dir/tool_a.py", "some/dir/tool_b.py"],
    ),
    (
        "star_bullets_and_decorated_tokens",
        _plan("* some/dir/\n  * `tool_a.py`\n  - **tool_b.py** (~10 lines)"),
        ["some/dir/", "some/dir/tool_a.py", "some/dir/tool_b.py"],
    ),
    (
        "dir_bullet_with_trailing_prose",
        _plan("- src/cw/ (new files)\n  - a.py"),
        ["src/cw/", "src/cw/a.py"],
    ),
    (
        "nested_files_with_own_slash_stay_as_written",
        _plan("- src/\n  - cw/x.py"),
        ["src/", "cw/x.py"],
    ),
    (
        "nested_dir_bullet_literal",
        _plan("- src/\n  - cw/\n    - x.py"),
        ["src/", "cw/", "cw/x.py"],
    ),
    (
        "same_indent_resets_context",
        _plan("- a/\n- b.py"),
        ["a/", "b.py"],
    ),
    (
        "lesser_indent_resets_context",
        _plan("  - a/\n    - x.py\n- y.py"),
        ["a/", "a/x.py", "y.py"],
    ),
    (
        "new_dir_replaces_context",
        _plan("- a/\n  - x.py\n- b/\n  - y.py"),
        ["a/", "a/x.py", "b/", "b/y.py"],
    ),
    (
        "prose_bullet_at_same_indent_resets",
        _plan("- a/\n- Note: untouched\n  - x.py"),
        ["a/", "x.py"],
    ),
    (
        "deeper_prose_bullet_does_not_reset",
        _plan("- a/\n  - some prose\n  - x.py"),
        ["a/", "a/x.py"],
    ),
    (
        "bare_filename_without_context",
        _plan("- tool_a.py"),
        ["tool_a.py"],
    ),
    (
        "tab_indent_nested",
        _plan("- a/\n\t- x.py"),
        ["a/", "a/x.py"],
    ),
    (
        "tab_indent_equal_resets",
        _plan("\t- a/\n\t- x.py"),
        ["a/", "x.py"],
    ),
    (
        "mixed_space_indent_widths",
        _plan("- a/\n  - x.py\n    - y.py\n- b/\n    - z.py"),
        ["a/", "a/x.py", "a/y.py", "b/", "b/z.py"],
    ),
    (
        "fenced_illustration_still_ignored",
        (
            "# Plan\n\n"
            "```\n## Files Modified\n\n- fenced/\n  - ghost.py\n```\n\n"
            "## Files Modified\n\n"
            "- real/\n"
            "  - one.py\n"
            "  ```\n  - fenced_bullet.py\n  ```\n"
            "  - two.py\n\n"
            "## Ambiguities\n\nNONE\n"
        ),
        ["real/", "real/one.py", "real/two.py"],
    ),
    (
        "table_rows_unchanged",
        _plan("- a/\n| File | Change |\n|---|---|\n| b.py | edit |\n  - x.py"),
        ["a/", "b.py", "x.py"],
    ),
    (
        "table_only_plan_unchanged",
        _plan(
            "| File | Change |\n|------|--------|\n"
            "| `src/cw/a.py` | edit |\n| tests/test_a.py | new |"
        ),
        ["src/cw/a.py", "tests/test_a.py"],
    ),
    (
        "dedup_after_resolution",
        _plan("- a/x.py\n- a/\n  - x.py"),
        ["a/x.py", "a/"],
    ),
    (
        "thirteen_file_shape",
        _plan(
            "- src/cw/tools/\n"
            + "\n".join(f"  - tool_{n}.py (~20 lines)" for n in range(13))
        ),
        ["src/cw/tools/", *_THIRTEEN],
    ),
]

_KITCHEN_SINK = _plan(
    "- a/\n  - x.py\n\t- tabbed.py\n- Note: prose\n  - bare.py\n"
    "```\n- fenced/\n  - ghost.py\n```\n"
    "- b/\n| File | Change |\n|---|---|\n| t.py | edit |\n  - after_table.py\n"
    "  - c/\n    - deep.py\n- tail.py"
)


@pytest.mark.parametrize("parser", _PARSERS)
@pytest.mark.parametrize(
    ("plan_text", "expected"),
    [pytest.param(text, want, id=case_id) for case_id, text, want in _CASES],
)
def test_nested_bullet_resolution(
    parser: Callable[[str], list[str]], plan_text: str, expected: list[str]
) -> None:
    assert parser(plan_text) == expected


def test_thirteen_file_shape_matches_diff_set() -> None:
    parsed = parse_plan_files_modified(_CASES[-1][1])
    assert set(parsed) - {"src/cw/tools/"} == set(_THIRTEEN)


_PARITY_TEXTS = [
    *(text for _id, text, _want in _CASES),
    _plan_text(["src/cw/a.py", "tests/test_a.py"]),
    _KITCHEN_SINK,
    "no files heading here\n",
]


@pytest.mark.parametrize("plan_text", _PARITY_TEXTS)
def test_both_parsers_agree_on_every_case(plan_text: str) -> None:
    assert _mod._parse_files_modified(plan_text) == parse_plan_files_modified(plan_text)


def test_kitchen_sink_resolution() -> None:
    assert parse_plan_files_modified(_KITCHEN_SINK) == [
        "a/",
        "a/x.py",
        "a/tabbed.py",
        "bare.py",
        "b/",
        "t.py",
        "after_table.py",
        "c/",
        "c/deep.py",
        "tail.py",
    ]


# ---------------------------------------------------------------------------
# Helper unit tests (both copies) so branch coverage never rides on the loop.
# ---------------------------------------------------------------------------

_HELPER_MODULES = [
    pytest.param(_mod, id="script"),
    pytest.param(plan_files, id="package"),
]


@pytest.mark.parametrize("module", _HELPER_MODULES)
@pytest.mark.parametrize(
    ("line", "width"),
    [
        ("", 0),
        ("- x", 0),
        ("    - x", 4),
        ("\t- x", 4),
        ("  \t- x", 4),
        ("\t  - x", 6),
    ],
)
def test_indent_width(module: types.ModuleType, line: str, width: int) -> None:
    assert module._indent_width(line) == width


@pytest.mark.parametrize("module", _HELPER_MODULES)
@pytest.mark.parametrize(
    ("token", "indent", "dir_ctx", "expected"),
    [
        # same-or-lesser indent resets the context, then the token is judged bare
        ("x.py", 0, (0, "a/"), ("x.py", None)),
        ("x.py", 0, (2, "a/"), ("x.py", None)),
        # prose bullet: no path; context kept when deeper, reset otherwise
        (None, 2, (0, "a/"), (None, (0, "a/"))),
        (None, 0, (0, "a/"), (None, None)),
        # directory bullet: literal token, new context
        ("b/", 2, (0, "a/"), ("b/", (2, "b/"))),
        ("b/", 0, None, ("b/", (0, "b/"))),
        # nested bare filename resolves against the directory
        ("x.py", 2, (0, "a/"), ("a/x.py", (0, "a/"))),
        # own slash: as written
        ("c/x.py", 2, (0, "a/"), ("c/x.py", (0, "a/"))),
        # no context: as written
        ("x.py", 2, None, ("x.py", None)),
    ],
)
def test_resolve_nested_path(
    module: types.ModuleType,
    token: str | None,
    indent: int,
    dir_ctx: tuple[int, str] | None,
    expected: tuple[str | None, tuple[int, str] | None],
) -> None:
    result = module._resolve_nested_path(token, indent, dir_ctx)
    assert result == expected
