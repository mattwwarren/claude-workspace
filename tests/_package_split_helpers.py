"""Shared AST and cold-import helpers for package-split guard tests (#3037).

A flat module split into a package by concern gets a guard test that walks the
package's source files: module-scope import discipline, the sibling-import DAG,
and a cold-interpreter import smoke test. This module is the one home for the
helpers those tests share, so no package test grows its own private copy.

The three package-dependent helpers take the dotted package name and work on
the flat file before the split and on the package directory after it.

``tests/test_review_finding_dispositions_package.py`` and
``tests/test_reconcile_dispatch_import_cycle.py`` still carry older private
copies of these helpers; they migrate onto this module in #3048.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import cw

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The ``src/cw`` directory every package name below resolves against.
SRC_ROOT = Path(cw.__file__).parent


def _package_root(package: str) -> Path:
    """``SRC_ROOT``-relative location of *package* (a dotted ``cw.*`` name)."""
    return SRC_ROOT.joinpath(*package.split(".")[1:])


def _package_files(package: str) -> list[Path]:
    """The source files of *package*: the flat file before the split."""
    root = _package_root(package)
    if root.is_dir():
        return sorted(root.rglob("*.py"))
    return [root.parent / f"{root.name}.py"]


def _module_name(path: Path, package: str) -> str:
    """The dotted module name of one of *package*'s source files."""
    root = _package_root(package)
    if path.name == "__init__.py" or path == root.parent / f"{root.name}.py":
        return package
    return f"{package}.{path.stem}"


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _runtime_imports(
    statements: list[ast.stmt],
) -> Iterator[ast.Import | ast.ImportFrom]:
    """Imports that run when the module loads.

    Function bodies run later, and ``if TYPE_CHECKING:`` bodies never run, so
    both are skipped; class bodies and other compound statements run at import
    time and are walked.
    """
    for stmt in statements:
        if isinstance(stmt, ast.Import | ast.ImportFrom):
            yield stmt
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        elif isinstance(stmt, ast.If) and _is_type_checking(stmt.test):
            yield from _runtime_imports(stmt.orelse)
        else:
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    yield from _runtime_imports([child])
                elif isinstance(child, ast.excepthandler):
                    yield from _runtime_imports(child.body)


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    # A relative import has no absolute name; report it so it is never allowed.
    return [("." * node.level) + (node.module or "")]


def _is_cw(name: str) -> bool:
    return name == "cw" or name.startswith((".", "cw."))


def _sibling_modules(package: str) -> set[str]:
    """Every submodule of *package* (empty for the flat file)."""
    return {_module_name(path, package) for path in _package_files(package)} - {package}


def _run_cold(script: str) -> None:
    """Run *script* in a fresh isolated interpreter; raise if it fails.

    Uses ``sys.executable`` (not a bare ``python3``) per PYTHON-PATTERNS'
    compiled-dependency isolation rule: ``pydantic_core`` is ABI-bound to this
    venv.
    """
    subprocess.run([sys.executable, "-I", "-c", script], check=True)
