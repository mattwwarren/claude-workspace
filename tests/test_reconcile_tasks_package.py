"""Package-split guards for ``cw.reconcile.tasks`` (#2631).

``cw.reconcile.tasks`` was one flat module and is being split into a package by
concern. These assertions hold against the flat module and must stay green
through every extraction commit, so the split is checked at each step rather
than only at the end. Assertions that cannot be true until a given extraction
exists are added in the commit that makes them true.

Template: ``tests/test_review_workspace_package.py`` (#3037); shared helpers come
from ``tests/_package_split_helpers.py``.
"""

from __future__ import annotations

import ast
import graphlib
from importlib import import_module
from typing import TYPE_CHECKING

import pytest

import cw.reconcile
import cw.reconcile.tasks as pkg
from tests._package_split_helpers import (
    SRC_ROOT,
    _module_name,
    _package_files,
    _run_cold,
    _runtime_imports,
    _sibling_modules,
)

if TYPE_CHECKING:
    from pathlib import Path

PACKAGE = "cw.reconcile.tasks"

# The full public-and-private surface of the flat module: 8 constants and 26
# functions. Hardcoded on purpose: deriving it from the package would let a
# moved or dropped name pass silently.
EXPECTED_EXPORTS = frozenset(
    {
        "_COMPLETION_ROUTING_GRACE_SECONDS",
        "_PROPOSED_ACTION_VARIANT_A",
        "_PROPOSED_ACTION_VARIANT_B",
        "_STALE_GATE_CONSUMER",
        "_TERMINAL_SIBLING_STATUSES",
        "_VARIANT_A_BLOCKED_DISPOSITION",
        "_VARIANT_A_MERGE_PENDING_DISPOSITION",
        "_VARIANT_B_DISPOSITION",
        "_client_cwd",
        "_closed_pr_numbers_by_client",
        "_collect_timed_out_merged_candidates",
        "_emit_never_claimed_refusals",
        "_filter_merged_candidates",
        "_index_variant_a_candidates",
        "_is_dangling_client",
        "_is_never_claimed",
        "_is_variant_a_gate_task",
        "_is_variant_b_gate_task",
        "_latest_terminal_at_index",
        "_merged_pr_numbers_by_client",
        "_pr_numbers_by_client_in_state",
        "_release_variant_a",
        "_release_variant_b",
        "_resolve_task_policy",
        "_revert_running_tasks_for_sessions",
        "_revert_terminal_backstop",
        "_sessions_past_completion_grace",
        "backstop_targets",
        "capture_backstop_dirty_checks",
        "complete_timed_out_merged_tasks",
        "park_terminal_sibling_tasks",
        "release_stale_gated_tasks",
        "revert_completed_silent_tasks",
        "revert_timed_out_tasks",
    }
)

# Names the public ``cw.reconcile`` package re-exports from this package.
PUBLIC_EXPORTS = frozenset(
    {
        "complete_timed_out_merged_tasks",
        "park_terminal_sibling_tasks",
        "release_stale_gated_tasks",
        "revert_completed_silent_tasks",
        "revert_timed_out_tasks",
    }
)

# Owning module of each name. Starts as the flat module for every name; each
# extraction commit repoints its names to the submodule that now defines them.
_OWNER: dict[str, str] = dict.fromkeys(EXPECTED_EXPORTS, PACKAGE)
_OWNER.update(
    dict.fromkeys(
        ("_client_cwd", "_is_dangling_client", "_resolve_task_policy"),
        f"{PACKAGE}._client_config",
    )
)
_OWNER.update(
    dict.fromkeys(
        (
            "_COMPLETION_ROUTING_GRACE_SECONDS",
            "_revert_running_tasks_for_sessions",
            "_sessions_past_completion_grace",
            "_revert_terminal_backstop",
            "backstop_targets",
            "capture_backstop_dirty_checks",
            "revert_completed_silent_tasks",
            "revert_timed_out_tasks",
        ),
        f"{PACKAGE}.backstops",
    )
)

_OWNER.update(
    dict.fromkeys(
        (
            "_TERMINAL_SIBLING_STATUSES",
            "_latest_terminal_at_index",
            "park_terminal_sibling_tasks",
        ),
        f"{PACKAGE}.terminal_siblings",
    )
)

_OWNER.update(
    dict.fromkeys(
        (
            "_collect_timed_out_merged_candidates",
            "_emit_never_claimed_refusals",
            "_filter_merged_candidates",
            "_is_never_claimed",
            "complete_timed_out_merged_tasks",
        ),
        f"{PACKAGE}.merged_completion",
    )
)

# (reader, module global it reads, owning module): the reader's globals must be
# the owner's namespace, so a patch on the owner reaches the reader.
PATCH_OWNERSHIP: tuple[tuple[str, str, str], ...] = (
    ("_revert_terminal_backstop", "partition_dirty", f"{PACKAGE}.backstops"),
    ("park_terminal_sibling_tasks", "load_clients", f"{PACKAGE}.terminal_siblings"),
)

# Importers of this package and the names each binds at module scope. Each
# binding must be the same object the package exposes.
CONSUMER_IMPORTS: dict[str, frozenset[str]] = {
    "cw.reconcile": PUBLIC_EXPORTS,
    "cw.reconcile.core": frozenset({"complete_timed_out_merged_tasks"}),
    "cw.reconcile.locked_sweeps": frozenset(
        {
            "park_terminal_sibling_tasks",
            "revert_completed_silent_tasks",
            "revert_timed_out_tasks",
        }
    ),
    "cw.reconcile.prepasses": frozenset(
        {"_client_cwd", "_is_dangling_client", "capture_backstop_dirty_checks"}
    ),
    "cw.reconcile.stale_dispatch_watch": frozenset(
        {"_client_cwd", "_is_dangling_client", "_is_variant_b_gate_task"}
    ),
    "cw.reconcile.local": frozenset({"_resolve_task_policy"}),
    "cw.reconcile.codex_boot": frozenset({"_resolve_task_policy"}),
    "cw.reconcile.unowned_running_acts": frozenset({"_resolve_task_policy"}),
    "cw.reconcile.shipped_release": frozenset({"_client_cwd"}),
    "cw.reconcile.gate_recipe_comments": frozenset(
        {"_client_cwd", "_is_dangling_client"}
    ),
    "cw.doctor.loop_health": frozenset({"_client_cwd"}),
    "cw.cw_pr_events_channel": frozenset({"_client_cwd", "_is_dangling_client"}),
}

# Modules that must import cold, in a fresh interpreter, on their own. Each
# extraction commit appends its submodule here.
COLD_IMPORTS: tuple[str, ...] = (
    PACKAGE,
    f"{PACKAGE}._client_config",
    f"{PACKAGE}.backstops",
    f"{PACKAGE}.terminal_siblings",
    f"{PACKAGE}.merged_completion",
)


# Deliberate local copies: ``_parse`` and ``_top_level_names`` also live in
# tests/test_review_workspace_package.py. Hoisting them into
# tests/_package_split_helpers.py is separate debt, not done here.
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_names(tree: ast.Module) -> set[str]:
    """Project-defined top-level names: defs, classes and assignments.

    ``__all__`` is excluded, as the flat module has none and the package adds it
    deliberately.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    names.discard("__all__")
    return names


def test_name_set_matches_hardcoded_surface() -> None:
    """The package's project-defined top-level names are exactly the 34 names."""
    defined: set[str] = set()
    for path in _package_files(PACKAGE):
        defined |= _top_level_names(_parse(path))
    assert defined == set(EXPECTED_EXPORTS)
    for name in EXPECTED_EXPORTS:
        assert hasattr(pkg, name), name


# Deliberate local copy of the isort-style key that sorts ``__all__`` the way
# ruff RUF022 does. The same key is copied in tests/test_doctor_wedge_reexports.py.
def _isort_style_key(name: str) -> tuple[int, str]:
    stripped = name.lstrip("_")
    if stripped.isupper():
        return 0, name
    if stripped[:1].isupper():
        return 1, name
    return 2, name


def test_package_all_is_complete_and_sorted() -> None:
    """``__all__`` names the full surface, in isort style."""
    assert set(pkg.__all__) == set(EXPECTED_EXPORTS)
    assert list(pkg.__all__) == sorted(pkg.__all__, key=_isort_style_key)


def test_flat_module_file_is_gone() -> None:
    """The flat module is now the package ``__init__``; no ``tasks.py`` remains."""
    assert not (SRC_ROOT / "reconcile" / "tasks.py").exists()


def test_owner_table_covers_surface() -> None:
    """Every name has an owner that binds the object the package exposes."""
    assert set(_OWNER) == set(EXPECTED_EXPORTS)
    for name, owner in _OWNER.items():
        owner_ns = vars(import_module(owner))
        assert owner_ns[name] is getattr(pkg, name), name


@pytest.mark.parametrize("importer", sorted(CONSUMER_IMPORTS))
def test_consumer_binds_same_objects(importer: str) -> None:
    """Every importer still binds the same objects as the package."""
    namespace = vars(import_module(importer))
    for name in CONSUMER_IMPORTS[importer]:
        assert namespace[name] is getattr(pkg, name), f"{importer}.{name}"


class TestPatchOwnership:
    """A patch on a submodule global reaches the function that reads it."""

    @pytest.mark.parametrize(("reader", "global_name", "owner"), PATCH_OWNERSHIP)
    def test_reader_globals_are_owner_namespace(
        self, reader: str, global_name: str, owner: str
    ) -> None:
        owner_ns = vars(import_module(owner))
        assert getattr(pkg, reader).__globals__ is owner_ns
        assert global_name in owner_ns


def test_public_exports_in_cw_reconcile_all() -> None:
    """``cw.reconcile.__all__`` still carries the five public task functions."""
    assert set(cw.reconcile.__all__) >= PUBLIC_EXPORTS


def _sibling_dependencies(package: str) -> dict[str, set[str]]:
    """Runtime sibling-import edges, one entry per submodule of *package*."""
    siblings = _sibling_modules(package)
    graph: dict[str, set[str]] = {}
    for path in _package_files(package):
        module = _module_name(path, package)
        if module == package:
            continue
        deps: set[str] = set()
        for node in _runtime_imports(_parse(path).body):
            if isinstance(node, ast.ImportFrom) and node.level:
                # A relative import: ``from .x import y`` or ``from . import x``.
                if node.module:
                    deps.add(f"{package}.{node.module}")
                else:
                    deps.update(f"{package}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                deps.add(node.module)
            elif isinstance(node, ast.Import):
                deps.update(alias.name for alias in node.names)
        graph[module] = deps & siblings
    return graph


def test_sibling_import_graph_is_acyclic() -> None:
    """Submodules import each other in one direction only: no cycles."""
    graph = _sibling_dependencies(PACKAGE)
    # Raises graphlib.CycleError on a cycle; empty pre-split, real after.
    list(graphlib.TopologicalSorter(graph).static_order())


@pytest.mark.parametrize("module", COLD_IMPORTS)
def test_module_imports_cold(module: str) -> None:
    """Each module imports on its own in a fresh interpreter."""
    _run_cold(f"import {module}")


def test_no_module_binds_a_logger() -> None:
    """The flat module has no logger; a future one forces a pinned-name test."""
    for path in _package_files(PACKAGE):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import | ast.ImportFrom):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                assert "logging" not in modules, path
            elif isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "")
                )
                assert name != "getLogger", path
