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
import inspect
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

# Names each submodule defines, one explicit group per owner (#3470). No
# name is seeded with the package itself: every name is listed exactly once,
# under the submodule that defines it, which
# ``test_owner_groups_are_disjoint_and_cover_the_surface`` pins.
_CLIENT_CONFIG_NAMES = ("_client_cwd", "_is_dangling_client", "_resolve_task_policy")
_BACKSTOP_NAMES = (
    "_COMPLETION_ROUTING_GRACE_SECONDS",
    "_revert_running_tasks_for_sessions",
    "_sessions_past_completion_grace",
    "_revert_terminal_backstop",
    "backstop_targets",
    "capture_backstop_dirty_checks",
    "revert_completed_silent_tasks",
    "revert_timed_out_tasks",
)
_TERMINAL_SIBLING_NAMES = (
    "_TERMINAL_SIBLING_STATUSES",
    "_latest_terminal_at_index",
    "park_terminal_sibling_tasks",
)
_MERGED_COMPLETION_NAMES = (
    "_collect_timed_out_merged_candidates",
    "_emit_never_claimed_refusals",
    "_filter_merged_candidates",
    "_is_never_claimed",
    "complete_timed_out_merged_tasks",
)
_STALE_GATE_NAMES = (
    "_STALE_GATE_CONSUMER",
    "_VARIANT_A_MERGE_PENDING_DISPOSITION",
    "_VARIANT_A_BLOCKED_DISPOSITION",
    "_VARIANT_B_DISPOSITION",
    "_PROPOSED_ACTION_VARIANT_A",
    "_PROPOSED_ACTION_VARIANT_B",
    "_is_variant_a_gate_task",
    "_is_variant_b_gate_task",
    "_index_variant_a_candidates",
    "_pr_numbers_by_client_in_state",
    "_merged_pr_numbers_by_client",
    "_closed_pr_numbers_by_client",
    "_release_variant_a",
    "_release_variant_b",
    "release_stale_gated_tasks",
)
_OWNED_NAMES: dict[str, tuple[str, ...]] = {
    f"{PACKAGE}._client_config": _CLIENT_CONFIG_NAMES,
    f"{PACKAGE}.backstops": _BACKSTOP_NAMES,
    f"{PACKAGE}.terminal_siblings": _TERMINAL_SIBLING_NAMES,
    f"{PACKAGE}.merged_completion": _MERGED_COMPLETION_NAMES,
    f"{PACKAGE}.stale_gate": _STALE_GATE_NAMES,
}

# Owning module of each name.
_OWNER: dict[str, str] = {
    name: owner for owner, names in _OWNED_NAMES.items() for name in names
}

# The constants of the surface, by the same rule ``_isort_style_key`` uses.
_CONSTANTS = frozenset(n for n in EXPECTED_EXPORTS if n.lstrip("_").isupper())

# Line ceiling for every package module (CLAUDE.md "Module Size").
_MODULE_LINE_LIMIT = 800

# Flat-module globals that the split moved out of the package namespace.
PATCHED_GLOBALS = frozenset(
    {"load_clients", "load_orchestrator_config", "save_dev_queue", "partition_dirty"}
)

# (reader, module global it reads, owning module): the reader's globals must be
# the owner's namespace, so a patch on the owner reaches the reader.
# (reader, module global it reads, owning module): the reader's globals must be
# the owner's namespace, so a patch on the owner reaches the reader.
PATCH_OWNERSHIP: tuple[tuple[str, str, str], ...] = (
    ("_revert_terminal_backstop", "partition_dirty", f"{PACKAGE}.backstops"),
    ("park_terminal_sibling_tasks", "load_clients", f"{PACKAGE}.terminal_siblings"),
    ("release_stale_gated_tasks", "load_orchestrator_config", f"{PACKAGE}.stale_gate"),
    ("release_stale_gated_tasks", "load_clients", f"{PACKAGE}.stale_gate"),
    ("release_stale_gated_tasks", "save_dev_queue", f"{PACKAGE}.stale_gate"),
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
    f"{PACKAGE}.stale_gate",
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


def test_every_module_is_under_the_line_limit() -> None:
    """Each package module stays under the ~800-line yellow flag."""
    for path in _package_files(PACKAGE):
        count = len(path.read_text(encoding="utf-8").splitlines())
        assert count < _MODULE_LINE_LIMIT, f"{path.name}: {count} lines"


def test_package_init_is_a_pure_reexport() -> None:
    """``__init__`` defines nothing: it binds ``__all__`` names and submodules only."""
    init = _parse(SRC_ROOT / "reconcile" / "tasks" / "__init__.py")
    assert not any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        for node in init.body
    )
    submodules = {module.rsplit(".", 1)[1] for module in _sibling_modules(PACKAGE)}
    bound = {name for name in vars(pkg) if not name.startswith("__")}
    assert bound == set(pkg.__all__) | submodules


def test_package_binds_no_patched_global() -> None:
    """A stale ``cw.reconcile.tasks.<global>`` patch must fail, not pass silently."""
    init = _parse(SRC_ROOT / "reconcile" / "tasks" / "__init__.py")
    # Globals __init__'s own code reads are exempt: patching them still reaches
    # a reader defined there. Derived from the AST, not hardcoded.
    read_by_init = {
        node.id
        for node in ast.walk(init)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    moved = PATCHED_GLOBALS - read_by_init
    assert moved, "no moved global to check: the guard would be vacuous"
    for name in moved:
        assert name not in vars(pkg), name


def test_flat_module_file_is_gone() -> None:
    """The flat module is now the package ``__init__``; no ``tasks.py`` remains."""
    assert not (SRC_ROOT / "reconcile" / "tasks.py").exists()


def test_owner_groups_are_disjoint_and_cover_the_surface() -> None:
    """#3470: each name is listed once, under a submodule, never the package."""
    listed = sum(len(names) for names in _OWNED_NAMES.values())
    assert listed == len(_OWNER) == len(EXPECTED_EXPORTS)
    assert set(_OWNER) == set(EXPECTED_EXPORTS)
    assert PACKAGE not in set(_OWNER.values())


def test_every_name_is_defined_where_the_owner_table_says() -> None:
    """#3471: each package file defines exactly the names ``_OWNER`` assigns
    to it. A list, not a set, so a name defined in two files fails."""
    defined = [
        (name, _module_name(path, PACKAGE))
        for path in _package_files(PACKAGE)
        for name in _top_level_names(_parse(path))
    ]
    assert sorted(defined) == sorted(_OWNER.items())


def test_constant_set_is_the_eight_flat_module_constants() -> None:
    assert len(_CONSTANTS) == 8


@pytest.mark.parametrize("name", sorted(_OWNER.keys() - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """#3471: functions report their owning module as ``__module__``, so a
    re-import alias in the owner cannot stand in for a definition."""
    obj = vars(import_module(_OWNER[name]))[name]
    assert inspect.isfunction(obj) or inspect.isclass(obj)
    assert obj.__module__ == _OWNER[name]


def _resolve_import_from(node: ast.ImportFrom, module: str) -> str:
    """The absolute module an ``ImportFrom`` in submodule *module* reads from."""
    if not node.level:
        return node.module or ""
    base = module.rsplit(".", node.level)[0]
    return f"{base}.{node.module}" if node.module else base


def _bare_package_imports(source: str, module: str) -> list[int]:
    """Lines of *source* (submodule *module*) that import the bare package.

    Resolves ``import cw.reconcile.tasks``, ``from cw.reconcile.tasks import
    x``, ``from cw.reconcile import tasks`` and the relative ``from . import
    x``, anywhere in the AST (function-local imports included). A direct
    submodule path is not the bare package.
    """
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hit = any(alias.name == PACKAGE for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import_from(node, module)
            hit = base == PACKAGE or any(
                f"{base}.{alias.name}" == PACKAGE for alias in node.names
            )
        else:
            continue
        if hit:
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from cw.reconcile.tasks import x\n", [1]),
        ("import cw.reconcile.tasks\n", [1]),
        ("from cw.reconcile import tasks\n", [1]),
        ("from . import backstops\n", [1]),
        ("from .. import tasks\n", [1]),
        ("def f() -> None:\n    from cw.reconcile.tasks import x\n", [2]),
        ("from cw.reconcile.tasks._client_config import x\n", []),
        ("from ._client_config import x\n", []),
        ("import cw.reconcile.tasks.backstops\n", []),
    ],
)
def test_bare_package_import_detector_catches_each_form(
    source: str, expected: list[int]
) -> None:
    """#3472: the detector is demonstrably RED-capable for every form."""
    assert _bare_package_imports(source, f"{PACKAGE}.backstops") == expected


@pytest.mark.parametrize(
    "path",
    [p for p in _package_files(PACKAGE) if p.name != "__init__.py"],
    ids=lambda path: path.name,
)
def test_no_submodule_imports_the_bare_package(path: Path) -> None:
    """#3472: siblings reach each other by direct path, never through the
    package, which would make the import order load-bearing."""
    source = path.read_text(encoding="utf-8")
    assert _bare_package_imports(source, _module_name(path, PACKAGE)) == []


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
