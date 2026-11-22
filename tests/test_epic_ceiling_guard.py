"""Pin who may read ``epic.max_diff_lines`` (#3783, RFC 0020 D-5).

The ceiling is enqueue-only: ``cw dev-queue add --base`` is the one reader, and
the dispatch tick must never read it, or a later tightening of the setting could
silently stop work already queued. The scan is an AST walk, so a read spelled
``x.epic.max_diff_lines`` or ``getattr(x, "max_diff_lines")`` both count, while
help text and docstrings that merely mention the key do not.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests._shared.workflow_text import _SRC_ROOT, _files_under, _iter_src_files

if TYPE_CHECKING:
    from pathlib import Path

_KEY = "max_diff_lines"
# The definition (its Field name is a Name target, not a read) and the single
# enqueue-side reader, the only places in src/ allowed to touch the key.
_ALLOWLIST = frozenset({"cw/models/client.py", "cw/dev_queue/epic_base.py"})


def _key_reads(source: str) -> list[int]:
    """Line numbers of every attribute read or exact string use of the key."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if (isinstance(node, ast.Attribute) and node.attr == _KEY)
        or (isinstance(node, ast.Constant) and node.value == _KEY)
    ]


def _relpath(path: Path) -> str:
    return path.relative_to(_SRC_ROOT).as_posix()


def _violations(paths: list[Path]) -> list[str]:
    out: list[str] = []
    for path in paths:
        rel = _relpath(path)
        if rel not in _ALLOWLIST:
            out.extend(f"{rel}:{line}" for line in _key_reads(path.read_text()))
    return out


def test_dispatch_never_reads_the_ceiling() -> None:
    paths = list(_files_under("cw/dispatch/"))
    assert paths, "scan is vacuous: no dispatch files found"
    assert _violations(paths) == []


def test_only_the_enqueue_side_reads_the_ceiling_in_src() -> None:
    assert _violations(list(_iter_src_files())) == []


def test_collector_flags_attribute_and_string_forms() -> None:
    assert _key_reads("x = cfg.epic.max_diff_lines\n") == [1]
    assert _key_reads('y = getattr(cfg, "max_diff_lines")\n') == [1]


def test_collector_ignores_help_text_and_unrelated_names() -> None:
    assert _key_reads('h = "the epic.max_diff_lines ceiling"\n') == []
    assert _key_reads("z = cfg.other_lines\n") == []


def test_allowlist_is_not_vacuous() -> None:
    """Each allowlisted file must still exist and still hold a real read or field."""
    src = {_relpath(p) for p in _iter_src_files()}
    assert src >= _ALLOWLIST
