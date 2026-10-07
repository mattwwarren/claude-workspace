"""Shrinking allowlist for ``cw.reconcile``'s deferred ``cw.dispatch`` imports (#2613).

``cw.dispatch``'s package ``__init__`` imports ``cw.reconcile`` at module top,
so every reconcile reach back into ``cw.dispatch`` has to be a function-level
import, and every such module carries a PLC0415 per-file-ignore. #2613 moved
the pure queue-row helpers those imports reached for into the ``cw.queue_rows``
and ``cw.claim_evidence`` leaves and hoisted the imports to module scope.

This file pins what is left, the way ``SUBPROCESS_UNDER_SESSIONS_ALLOWLIST``
(``tests/_lock_invariants.py``) pins its own exceptions: an allowlist that may
only shrink. Adding a new deferred ``cw.dispatch`` import, or leaving a stale
PLC0415 ignore behind after a hoist, fails here.

The allowlist is a multiset keyed by enclosing function, not a set of
``(file, module, names)``: ``tasks.py`` once held three textually identical
deferrals in three functions, and a plain set would let one of them be removed
(or re-added) unnoticed.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from collections import Counter
from typing import TYPE_CHECKING

import pytest

from tests.conftest import _REPO_ROOT, _SRC_ROOT

if TYPE_CHECKING:
    from pathlib import Path

_RECONCILE_ROOT = _SRC_ROOT / "cw" / "reconcile"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"

_ImportKey = tuple[str, str, str, tuple[str, ...]]

# (relpath, innermost enclosing function, module, imported names) of every
# function-level ``cw.dispatch`` import under ``src/cw/reconcile/``. May only
# shrink.
_DEFERRED_DISPATCH_IMPORTS: Counter[_ImportKey] = Counter(
    {
        (
            "src/cw/reconcile/_shared/_routing.py",
            "_apply_sentinel_to_task",
            "cw.dispatch",
            ("_route_staged_decision", "apply_staged_decision"),
        ): 1,
        (
            "src/cw/reconcile/_shared/_sentinels.py",
            "classify_sentinel_stage_position",
            "cw.dispatch",
            ("_classify_sentinel_stage_position",),
        ): 1,
    }
)

# Every other function-level import under ``src/cw/reconcile/``. These are not
# ``cw.dispatch`` cycle deferrals; each is justified by its own pyproject
# comment, and pinning them here keeps those comments from going stale.
_DEFERRED_OTHER_IMPORTS: Counter[_ImportKey] = Counter(
    {
        (
            "src/cw/reconcile/codex_boot.py",
            "reap_orphaned_codex_sessions_at_boot",
            "cw.executor",
            ("resolve_executor_config",),
        ): 1,
        (
            "src/cw/reconcile/review_recipes/address_review.py",
            "_dispatch_address_review",
            "cw.spawn",
            ("spawn_create_impl",),
        ): 1,
        (
            "src/cw/reconcile/review_recipes/auto_fix_ci.py",
            "_requeue_existing_row",
            "cw.dev_queue",
            ("requeue_ticket",),
        ): 1,
        (
            "src/cw/reconcile/review_recipes/auto_fix_ci.py",
            "_dispatch_auto_fix_ci",
            "cw.dev_queue",
            ("classify_requeue_live_session_error",),
        ): 1,
        (
            "src/cw/reconcile/review_recipes/fix_agent.py",
            "dispatch_fix_agent",
            "cw.spawn",
            ("spawn_create_impl",),
        ): 1,
        (
            "src/cw/reconcile/review_recipes/request_reviewer.py",
            "_dispatch_request_reviewer",
            "cw.gh",
            ("add_pr_reviewer",),
        ): 1,
    }
)

# The ``src/cw/reconcile/`` keys of ``[tool.ruff.lint.per-file-ignores]`` that
# ignore PLC0415. May only shrink.
_RECONCILE_PLC0415_KEYS = frozenset(
    {
        "src/cw/reconcile/_shared/_routing.py",
        "src/cw/reconcile/_shared/_sentinels.py",
        "src/cw/reconcile/codex_boot.py",
        "src/cw/reconcile/review_recipes/address_review.py",
        "src/cw/reconcile/review_recipes/fix_agent.py",
        "src/cw/reconcile/review_recipes/auto_fix_ci.py",
        "src/cw/reconcile/review_recipes/request_reviewer.py",
    }
)

# Reconcile modules #2613 hoists ``cw.dispatch`` imports in, plus the two
# packages on either side of the cycle. Each must import first in a fresh
# interpreter.
_COLD_IMPORT_FIRST = (
    "cw.reconcile",
    "cw.dispatch",
    "cw.reconcile.codex_boot",
    "cw.reconcile.fix_dispatch",
    "cw.reconcile.gate_recipes",
    "cw.reconcile.local",
    "cw.reconcile.phantom._mutations",
    "cw.reconcile.stalled._mutations",
    "cw.reconcile.tasks",
    "cw.reconcile.unowned_running",
    "cw.reconcile.usage_limit_mid_turn",
)


# The two retained routing-engine deferrals. Their shared pyproject comment
# must cite the dependency-inversion follow-up that would let them go.
_ROUTING_ENGINE_KEYS = (
    "src/cw/reconcile/_shared/_routing.py",
    "src/cw/reconcile/_shared/_sentinels.py",
)
_ROUTING_FOLLOW_UP = "#2619"


def _pyproject_comment_above(key: str) -> str:
    """The ``#`` comment block above *key*'s per-file-ignore line.

    Sibling key lines directly above *key* are skipped, so keys that share one
    comment block (the two ``_shared`` entries) resolve to the same comment.
    """
    lines = _PYPROJECT_PATH.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(f'"{key}"'))
    index -= 1
    while index >= 0 and lines[index].startswith('"'):
        index -= 1
    comment: list[str] = []
    while index >= 0 and lines[index].startswith("#"):
        comment.append(lines[index])
        index -= 1
    return "\n".join(reversed(comment))


def _is_dispatch_module(module: str) -> bool:
    return module == "cw.dispatch" or module.startswith("cw.dispatch.")


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_key(
    relpath: str, function: str, node: ast.Import | ast.ImportFrom
) -> _ImportKey:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        names = tuple(alias.name for alias in node.names)
    else:
        module = node.names[0].name
        names = tuple(alias.name for alias in node.names)
    return (relpath, function, module, names)


def _collect(node: ast.AST, relpath: str, function: str | None) -> list[_ImportKey]:
    """Function-scope imports under *node*, keyed by innermost enclosing function."""
    found: list[_ImportKey] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If) and _is_type_checking_guard(child):
            found.extend(
                key
                for branch in child.orelse
                for key in _collect(branch, relpath, function)
            )
            continue
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            found.extend(_collect(child, relpath, child.name))
            continue
        if function is not None and isinstance(child, ast.Import | ast.ImportFrom):
            found.append(_import_key(relpath, function, child))
        found.extend(_collect(child, relpath, function))
    return found


def _function_level_imports(path: Path) -> list[_ImportKey]:
    relpath = path.relative_to(_REPO_ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _collect(tree, relpath, None)


def _scan_reconcile() -> list[_ImportKey]:
    return [
        key
        for path in sorted(_RECONCILE_ROOT.rglob("*.py"))
        for key in _function_level_imports(path)
    ]


def _reconcile_plc0415_keys() -> frozenset[str]:
    with _PYPROJECT_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    ignores: dict[str, list[str]] = data["tool"]["ruff"]["lint"]["per-file-ignores"]
    return frozenset(
        key
        for key, codes in ignores.items()
        if key.startswith("src/cw/reconcile/") and "PLC0415" in codes
    )


def test_deferred_dispatch_imports_match_the_allowlist() -> None:
    """Every function-level ``cw.dispatch`` import in reconcile is allowlisted."""
    found = Counter(key for key in _scan_reconcile() if _is_dispatch_module(key[2]))
    assert found == _DEFERRED_DISPATCH_IMPORTS


def test_other_deferred_imports_are_pinned() -> None:
    """Every non-dispatch function-level import in reconcile is pinned."""
    found = Counter(key for key in _scan_reconcile() if not _is_dispatch_module(key[2]))
    assert found == _DEFERRED_OTHER_IMPORTS


def test_reconcile_plc0415_ignores_match_the_allowlist() -> None:
    """The reconcile PLC0415 per-file-ignores are exactly the allowlisted keys."""
    assert _reconcile_plc0415_keys() == _RECONCILE_PLC0415_KEYS


def test_every_plc0415_ignore_still_covers_a_deferred_import() -> None:
    """A retained ignore whose file has no function-level import is stale."""
    deferring_files = {
        key[0] for key in _DEFERRED_DISPATCH_IMPORTS + _DEFERRED_OTHER_IMPORTS
    }
    assert deferring_files == _RECONCILE_PLC0415_KEYS


def test_only_routing_engine_dispatch_deferrals_remain() -> None:
    """Every remaining ``cw.dispatch`` deferral is a routing-engine site."""
    assert {key[0] for key in _DEFERRED_DISPATCH_IMPORTS} == set(_ROUTING_ENGINE_KEYS)


@pytest.mark.parametrize("key", _ROUTING_ENGINE_KEYS)
def test_routing_engine_ignore_cites_follow_up(key: str) -> None:
    """The routing-engine PLC0415 comment cites the #2619 follow-up."""
    assert _ROUTING_FOLLOW_UP in _pyproject_comment_above(key)


@pytest.mark.parametrize("module", _COLD_IMPORT_FIRST)
def test_module_imports_first_in_a_cold_interpreter(module: str) -> None:
    """Importing *module* first in a fresh interpreter hits no partial cycle."""
    subprocess.run(
        [sys.executable, "-I", "-c", f"import {module}"],
        check=True,
        env=os.environ.copy(),
    )
