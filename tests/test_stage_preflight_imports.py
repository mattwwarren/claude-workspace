"""Import discipline of the ``cw.stage_preflight`` leaf (RFC 0019 H2, #3709).

The package sits below the executors, the dispatch tick and the doctor, so it
must never import ``cw.executor`` or ``cw.dispatch``: not at module scope, not
in a function body, not under ``TYPE_CHECKING``. A cold interpreter pins the
transitive half (``cw.models`` must not pull either in), and every module stays
at most 200 lines.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._package_split_helpers import _module_name, _package_files, _run_cold

_PACKAGE = "cw.stage_preflight"
_FORBIDDEN = ("cw.executor", "cw.dispatch")
_MAX_LINES = 200
_FILES = _package_files(_PACKAGE)


def _every_import(tree: ast.AST) -> list[str]:
    """Every imported module name at any nesting depth, relative ones dotted."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    return names


def _forbidden(name: str) -> bool:
    return any(name == root or name.startswith(f"{root}.") for root in _FORBIDDEN)


def test_the_package_has_its_four_modules_and_init() -> None:
    assert sorted(_module_name(path, _PACKAGE) for path in _FILES) == [
        "cw.stage_preflight",
        "cw.stage_preflight.decision",
        "cw.stage_preflight.details",
        "cw.stage_preflight.needs",
        "cw.stage_preflight.reasons",
    ]


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_no_module_imports_the_executor_or_dispatch(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = _every_import(tree)
    assert not [n for n in names if _forbidden(n)]
    assert not [n for n in names if n.startswith(".")]


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_each_module_stays_under_the_size_ceiling(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").count("\n")
    assert lines <= _MAX_LINES


def test_the_detector_flags_nested_and_type_checking_imports() -> None:
    """Positive control: a nested or ``TYPE_CHECKING`` import is still seen."""
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from cw.executor import core\n"
        "def f():\n"
        "    import cw.dispatch.lanes\n"
    )
    names = _every_import(ast.parse(source))
    assert [n for n in names if _forbidden(n)] == ["cw.executor", "cw.dispatch.lanes"]
    assert not _forbidden("cw.executorish")


def test_cold_import_loads_no_executor_or_dispatch_module() -> None:
    _run_cold(
        "import sys\n"
        "import cw.stage_preflight\n"
        "bad = sorted(m for m in sys.modules"
        " if m.startswith(('cw.executor', 'cw.dispatch')))\n"
        "assert not bad, bad\n"
    )


def test_cold_import_of_cw_models_loads_no_executor_or_dispatch_module() -> None:
    """The leaf may import ``cw.models`` only because it pulls neither in."""
    _run_cold(
        "import sys\n"
        "import cw.models\n"
        "bad = sorted(m for m in sys.modules"
        " if m.startswith(('cw.executor', 'cw.dispatch')))\n"
        "assert not bad, bad\n"
    )
