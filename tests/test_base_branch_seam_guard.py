"""Pin every ``TicketTask.base_branch`` assignment under ``src/`` to one writer (#3779).

:func:`cw.models.tasks.base_branch.set_base_branch` is the only place that may
assign the attribute, because it alone enforces the ``ever_spawned`` freeze.
A direct ``row.base_branch = ...`` anywhere else would bypass the freeze and
could retarget a row that already has a session's work on it.

Reads are out of scope: the attribute is read freely. Only Store context is
flagged, and the class-body annotation ``base_branch: str | None = None`` is a
plain ``Name`` target, not an attribute store, so it is not flagged either.
"""

from __future__ import annotations

import ast

from tests._shared.workflow_text import _SRC_ROOT, _iter_src_files

_ATTR = "base_branch"
_WRITER_MODULE = "cw/models/tasks/base_branch.py"
_ALLOWLIST = frozenset({_WRITER_MODULE})


def _assigned_base_branch_lines(source: str) -> list[int]:
    """Line numbers of every write to ``<expr>.base_branch`` in *source*.

    Counts attribute targets of ``=``, ``+=`` and annotated assignments, plus
    ``setattr(<x>, "base_branch", ...)`` calls.
    """
    tree = ast.parse(source)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Attribute) and target.attr == _ATTR
                for target in targets
            ):
                lines.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == _ATTR
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


def _violations() -> list[str]:
    found: list[str] = []
    for path in _iter_src_files():
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        if relpath in _ALLOWLIST:
            continue
        source = path.read_text(encoding="utf-8")
        hits = _assigned_base_branch_lines(source)
        found.extend(f"{relpath}:{line}" for line in hits)
    return found


def test_base_branch_is_assigned_only_by_the_helper() -> None:
    assert _violations() == []


def test_allowlist_is_not_vacuous() -> None:
    """The helper itself assigns, so the scan must find it there."""
    source = (_SRC_ROOT / "cw" / "models" / "tasks" / "base_branch.py").read_text(
        encoding="utf-8"
    )
    assert _assigned_base_branch_lines(source)


def test_collector_flags_attribute_and_setattr_forms() -> None:
    source = (
        "t.base_branch = x\n"
        "t.base_branch += x\n"
        "t.base_branch: str | None = x\n"
        'setattr(t, "base_branch", x)\n'
    )
    assert _assigned_base_branch_lines(source) == [1, 2, 3, 4]


def test_collector_ignores_reads_and_class_annotations() -> None:
    source = (
        "y = t.base_branch\n"
        "class TicketTask:\n"
        "    base_branch: str | None = None\n"
        "print(t.base_branch)\n"
    )
    assert _assigned_base_branch_lines(source) == []


def test_violations_are_empty_for_the_allowlisted_writer() -> None:
    source = "t.base_branch = x\n"
    assert _assigned_base_branch_lines(source)
    relpath = _WRITER_MODULE
    assert relpath in _ALLOWLIST
