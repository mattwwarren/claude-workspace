"""Seam assertions for the ``cw.review_workspace`` package split (#3037).

``review_workspace`` was a single 1432-line flat module. The split must keep
four things true that the behavioral suite cannot see on its own:

1. The historic names survive: every top-level name of the flat module is
   defined in the submodule the owner table names, and the 22-name import
   surface (9 public names plus the 13 private names
   ``tests/test_review_workspace.py`` reaches through the package) still
   resolves through ``cw.review_workspace``.
2. The import discipline holds: no package file imports anything from ``cw``
   at module scope beyond ``cw._lock_guard``, ``cw.atomic``, ``cw.exceptions``,
   ``cw.review_findings`` and its own sibling submodules by direct path, and
   the sibling imports form a DAG.
3. The lock keeps its historic ``"review_workspace"`` name.
4. Every patched name is patched where it is called. A test that patches
   ``_write_transaction`` (or another patched name) on one module while a
   second module also calls it stays green but covers fewer call sites; the
   call-site pin below fails instead.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import graphlib
import importlib
import inspect
from typing import TYPE_CHECKING

import pytest

import cw.atomic
from cw.review_workspace import (
    TransactionPhase,
    WorkspaceResult,
    finish_workspace,
    initialize_workspace,
    reconcile_workspace,
    record_document,
    record_failure,
    record_reprompt,
    recover_workspace,
)
from tests._package_split_helpers import (
    _imported_modules,
    _is_cw,
    _module_name,
    _package_files,
    _run_cold,
    _runtime_imports,
    _sibling_modules,
)
from tests.test_review_workspace import (
    _document,
    _marker,
    _seed_completed,
    _write_staging_round,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import ModuleType

_PKG = "cw.review_workspace"
_CONSTANTS_MODULE = f"{_PKG}._constants"
_MODELS = f"{_PKG}._models"
_PRIMITIVES = f"{_PKG}._primitives"
_LOCK = f"{_PKG}._lock"
_MARKER = f"{_PKG}._marker"
_MANIFEST = f"{_PKG}._manifest"
_RECOVERY = f"{_PKG}._recovery"
_ROUNDS = f"{_PKG}._rounds"

_ROLE = "Code Reviewer"

# Owning module for every historic top-level name of the flat
# ``review_workspace.py``. Each extraction commit of the split edits only the
# entries it moves.
_OWNER: dict[str, str] = {
    # Constants.
    "_ACTIVE_DIR": _CONSTANTS_MODULE,
    "_ACTIVE_MARKER": _CONSTANTS_MODULE,
    "_ARCHIVE_ROOT": _CONSTANTS_MODULE,
    "_STAGING_ROOT": _CONSTANTS_MODULE,
    "_LOCK_PATH": _CONSTANTS_MODULE,
    "_ID_RE": _CONSTANTS_MODULE,
    "_SLUG_RE": _CONSTANTS_MODULE,
    "_ROLE_STATES": _CONSTANTS_MODULE,
    "_TERMINAL_STATES": _CONSTANTS_MODULE,
    # Models.
    "TransactionPhase": _MODELS,
    "_Transaction": _MODELS,
    "_Marker": _MODELS,
    "WorkspaceResult": _MODELS,
    # Primitives.
    "_error": _PRIMITIVES,
    "_safe_component": _PRIMITIVES,
    "_role_slug": _PRIMITIVES,
    "_ensure_cw": _PRIMITIVES,
    "_kind": _PRIMITIVES,
    "_read_object": _PRIMITIVES,
    "_write_object": _PRIMITIVES,
    # Lock.
    "_workspace_lock": _LOCK,
    # Marker.
    "_raw_role_entries": _MARKER,
    "_string_list": _MARKER,
    "_parse_failures": _MARKER,
    "_parse_role_states": _MARKER,
    "_parse_marker": _MARKER,
    "_marker_payload": _MARKER,
    "_write_marker": _MARKER,
    "_change_marker": _MARKER,
    "_failure_map": _MARKER,
    "_documents": _MARKER,
    "_reconcile_marker": _MARKER,
    "_verify_bundle": _MARKER,
    "_reprompted_roles": _MARKER,
    # Manifest.
    "_transaction_paths": _MANIFEST,
    "_manifest_path": _MANIFEST,
    "_new_transaction": _MANIFEST,
    "_load_transaction": _MANIFEST,
    # Recovery.
    "_write_transaction": _RECOVERY,
    "_set_phase": _RECOVERY,
    "_rename": _RECOVERY,
    "_pair_state": _RECOVERY,
    "_expected_kind": _RECOVERY,
    "_verify_old": _RECOVERY,
    "_verify_new": _RECOVERY,
    "_verify_transaction_paths": _RECOVERY,
    "_recover_committed": _RECOVERY,
    "_verify_archived_new": _RECOVERY,
    "_recover_no_old": _RECOVERY,
    "_old_transaction_paths": _RECOVERY,
    "_recover_old_active_pair": _RECOVERY,
    "_recover_old_directory_moved": _RECOVERY,
    "_recover_old_archived": _RECOVERY,
    "_recover_old_new_directory": _RECOVERY,
    "_recover_old_new_pair": _RECOVERY,
    "_recover_old": _RECOVERY,
    "_recover_transaction": _RECOVERY,
    "_transaction_from_staging": _RECOVERY,
    "_transaction_roots": _RECOVERY,
    "_scan_archive_transactions": _RECOVERY,
    "_inspect_staging_entry": _RECOVERY,
    "_recover_staging_entries": _RECOVERY,
    "_recover_all": _RECOVERY,
    "recover_workspace": _RECOVERY,
    # Rounds.
    "_new_marker": _ROUNDS,
    "_workspace_result": _ROUNDS,
    "_active_pair": _ROUNDS,
    "_reconcile_active": _ROUNDS,
    "_validated_roles": _ROUNDS,
    "_resume_or_previous": _ROUNDS,
    "_start_round": _ROUNDS,
    "initialize_workspace": _ROUNDS,
    "reconcile_workspace": _ROUNDS,
    "_require_pending": _ROUNDS,
    "record_document": _ROUNDS,
    "record_failure": _ROUNDS,
    "record_reprompt": _ROUNDS,
    "finish_workspace": _ROUNDS,
}

# Everything in ``_OWNER`` that is not a function or a class. These carry no
# ``__module__`` of their own (or carry their type's), so their ownership is
# checked by the AST definition map instead.
_CONSTANTS = frozenset(
    {
        "_ACTIVE_DIR",
        "_ACTIVE_MARKER",
        "_ARCHIVE_ROOT",
        "_STAGING_ROOT",
        "_LOCK_PATH",
        "_ID_RE",
        "_SLUG_RE",
        "_ROLE_STATES",
        "_TERMINAL_STATES",
        "TransactionPhase",
    }
)

# The public surface: the seven names ``cw.cli.review.workspace`` imports plus
# the ``TransactionPhase`` alias.
_PUBLIC = (
    "TransactionPhase",
    "WorkspaceResult",
    "finish_workspace",
    "initialize_workspace",
    "reconcile_workspace",
    "record_document",
    "record_failure",
    "record_reprompt",
    "recover_workspace",
)

# The private names ``tests/test_review_workspace.py`` reaches through the
# package as ``workspace_module._x``.
_PRIVATE = (
    "_Transaction",
    "_change_marker",
    "_kind",
    "_new_marker",
    "_new_transaction",
    "_parse_marker",
    "_read_object",
    "_rename",
    "_verify_bundle",
    "_workspace_lock",
    "_write_marker",
    "_write_object",
    "_write_transaction",
)

# The ``cw`` modules a package file may import at module scope, besides its
# own siblings by direct path. The flat module imported exactly these.
_ALLOWED_CW_IMPORTS = frozenset(
    {"cw._lock_guard", "cw.atomic", "cw.exceptions", "cw.review_findings"}
)

# Every module the split produces (the package itself included), derived from
# the owner table so each extraction commit only has to edit ``_OWNER``.
_MODULES = tuple(sorted(set(_OWNER.values())))

# The first module a cold interpreter imports in the import-order smoke test.
_FIRST_IMPORTS = tuple(
    dict.fromkeys(("cw.cli.review.workspace", "cw.review_findings", *_MODULES))
)

# Every name a test patches, mapped to the top-level functions that call it by
# bare name (``fcntl.flock`` as an attribute call). A patch only reaches a call
# when it lands on the module that binds the name for that caller, so each
# caller below is checked against the module ``_OWNER`` places it in.
_CALLERS: dict[str, frozenset[str]] = {
    "_rename": frozenset(
        {
            "_recover_no_old",
            "_recover_old_active_pair",
            "_recover_old_directory_moved",
            "_recover_old_archived",
            "_recover_old_new_directory",
        }
    ),
    "_write_transaction": frozenset(
        {"_set_phase", "_transaction_from_staging", "_start_round"}
    ),
    "_write_marker": frozenset(
        {
            "_reconcile_marker",
            "_start_round",
            "record_document",
            "record_failure",
            "record_reprompt",
            "finish_workspace",
        }
    ),
    "atomic_write_text": frozenset({"_write_object", "record_document"}),
    "fcntl.flock": frozenset({"_workspace_lock"}),
}

_CALL_PAIRS = tuple(
    sorted((name, caller) for name, callers in _CALLERS.items() for caller in callers)
)


def _owner(name: str) -> ModuleType:
    return importlib.import_module(_OWNER[name])


def _owned(name: str) -> object:
    """The object *name* is bound to in its owning module's namespace."""
    return vars(_owner(name))[name]


def _owned_callable(name: str) -> Callable[..., object]:
    obj = _owned(name)
    assert callable(obj)
    return obj


def _module_file(module: str) -> Path:
    (path,) = [
        path for path in _package_files(_PKG) if _module_name(path, _PKG) == module
    ]
    return path


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Owner table and import surface
# ---------------------------------------------------------------------------


def _top_level_names(tree: ast.Module) -> set[str]:
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
    return names - {"__all__"}


def test_owner_table_covers_the_historic_surface() -> None:
    """The owner table names exactly the 76 historic top-level names."""
    assert len(_OWNER) == 78
    assert _CONSTANTS.issubset(_OWNER)
    assert set(_PUBLIC).issubset(_OWNER)
    assert set(_PRIVATE).issubset(_OWNER)


def test_every_name_is_defined_where_the_owner_table_says() -> None:
    """Each package file defines exactly the names ``_OWNER`` assigns to it."""
    defined = {
        (name, _module_name(path, _PKG))
        for path in _package_files(_PKG)
        for name in _top_level_names(_parse(path))
    }
    assert defined == set(_OWNER.items())


@pytest.mark.parametrize("name", sorted(_OWNER))
def test_historic_name_is_bound_in_its_owner(name: str) -> None:
    """Every historic name is an attribute of the module that owns it."""
    assert name in vars(_owner(name))


@pytest.mark.parametrize("name", sorted(_OWNER.keys() - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """Functions and classes report their owning module as ``__module__``."""
    obj = _owned(name)
    assert inspect.isfunction(obj) or inspect.isclass(obj)
    assert obj.__module__ == _OWNER[name]


@pytest.mark.parametrize("name", sorted(_CONSTANTS))
def test_constant_is_not_a_def(name: str) -> None:
    """The constant set holds no function or class (those are checked above)."""
    obj = _owned(name)
    assert not inspect.isfunction(obj)
    assert not inspect.isclass(obj)


@pytest.mark.parametrize("name", [*_PUBLIC, *_PRIVATE])
def test_surface_name_resolves_through_the_package(name: str) -> None:
    """``cw.review_workspace.X`` reaches the owner's X."""
    assert getattr(importlib.import_module(_PKG), name) is _owned(name)


def test_public_names_import_from_the_package() -> None:
    """``from cw.review_workspace import X`` works for every public name."""
    imported = {
        "TransactionPhase": TransactionPhase,
        "WorkspaceResult": WorkspaceResult,
        "finish_workspace": finish_workspace,
        "initialize_workspace": initialize_workspace,
        "reconcile_workspace": reconcile_workspace,
        "record_document": record_document,
        "record_failure": record_failure,
        "record_reprompt": record_reprompt,
        "recover_workspace": recover_workspace,
    }
    assert sorted(imported) == sorted(_PUBLIC)
    assert all(obj is _owned(name) for name, obj in imported.items())


# ---------------------------------------------------------------------------
# Import discipline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _package_files(_PKG), ids=lambda path: path.name)
def test_module_scope_cw_imports_are_the_flat_set_and_direct_siblings(
    path: Path,
) -> None:
    """No module-scope ``cw`` import beyond the flat module's own set.

    Sibling submodules are allowed by their direct path. Every other ``cw``
    import would be a new dependency the split introduced.
    """
    allowed = _ALLOWED_CW_IMPORTS | (
        _sibling_modules(_PKG) - {_module_name(path, _PKG)}
    )
    imported = {
        name
        for node in _runtime_imports(_parse(path).body)
        for name in _imported_modules(node)
        if _is_cw(name)
    }
    assert imported <= allowed, sorted(imported - allowed)


@pytest.mark.parametrize("path", _package_files(_PKG), ids=lambda path: path.name)
def test_no_package_file_imports_the_bare_package(path: Path) -> None:
    """Siblings reach each other by direct path, never through the package."""
    bare = [
        node.lineno
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.Import | ast.ImportFrom)
        and _PKG in _imported_modules(node)
    ]
    assert bare == []


def test_sibling_imports_form_a_dag() -> None:
    """No import cycle among the submodules (``__init__`` excluded)."""
    siblings = _sibling_modules(_PKG)
    graph = {
        _module_name(path, _PKG): {
            name
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in _imported_modules(node)
            if name in siblings
        }
        for path in _package_files(_PKG)
        if _module_name(path, _PKG) != _PKG
    }
    order = tuple(graphlib.TopologicalSorter(graph).static_order())
    assert set(order) == siblings


def test_lock_guard_keeps_the_historic_lock_name() -> None:
    """The ``lock_guard`` name stays the literal ``"review_workspace"``."""
    tree = _parse(_module_file(_OWNER["_workspace_lock"]))
    names = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "lock_guard"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert names == ["review_workspace"]


@pytest.mark.parametrize("first", _FIRST_IMPORTS)
def test_module_imports_cleanly_whichever_module_loads_first(first: str) -> None:
    """Import-order smoke test, one cold interpreter per first import."""
    _run_cold(
        f"import {first}\n"
        "import cw.review_workspace\n"
        "import cw.cli.review.workspace\n"
        "from cw.review_workspace import initialize_workspace\n"
        "assert callable(initialize_workspace)\n"
    )


# ---------------------------------------------------------------------------
# Call-site pin: each patched name is bound where it is called
# ---------------------------------------------------------------------------


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def _call_sites() -> dict[str, set[tuple[str, str]]]:
    """For each patched name, the (caller, module) pairs that call it."""
    sites: dict[str, set[tuple[str, str]]] = {name: set() for name in _CALLERS}
    for path in _package_files(_PKG):
        module = _module_name(path, _PKG)
        for node in _parse(path).body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = _called_name(call)
                if name in sites:
                    sites[name].add((node.name, module))
    return sites


def _expected_binding(binding: str) -> object:
    """What a caller's global *binding* must be: the original, never a copy."""
    if binding == "fcntl":
        return fcntl
    if binding == "atomic_write_text":
        return cw.atomic.atomic_write_text
    return _owned(binding)


def test_call_sites_are_exactly_the_pinned_callers() -> None:
    """A new caller, or a caller moved to another module, fails here."""
    expected = {
        name: {(caller, _OWNER[caller]) for caller in callers}
        for name, callers in _CALLERS.items()
    }
    assert _call_sites() == expected


@pytest.mark.parametrize(("name", "caller"), _CALL_PAIRS)
def test_patched_name_is_bound_in_the_callers_module(name: str, caller: str) -> None:
    """The caller resolves the patched name in its owner module's globals."""
    fn = inspect.unwrap(_owned_callable(caller))
    binding = name.split(".", 1)[0]
    assert fn.__globals__ is vars(_owner(caller))
    assert fn.__globals__[binding] is _expected_binding(binding)


def _first_run(root: Path) -> None:
    """No old round, so recovery runs ``_recover_no_old`` and ``_set_phase``."""
    initialize_workspace(root, ticket="T-1", round_id="round-1", roles=[_ROLE])


def _archived_round(root: Path) -> None:
    """An old pair exists, so recovery walks every ``_recover_old_*`` handler."""
    _seed_completed(root)
    initialize_workspace(root, ticket="T-2", round_id="round-2", roles=[_ROLE])


def _returns(root: Path) -> None:
    """``record_document``, ``record_failure`` and ``finish_workspace``."""
    _seed_completed(root)


def _reprompted(root: Path) -> None:
    """A pending role's first frame-missing re-prompt grant is recorded (#3096)."""
    _first_run(root)
    record_reprompt(root, role=_ROLE)


def _reconcile_returned(root: Path) -> None:
    """A findings file written behind the marker's back is reconciled."""
    _first_run(root)
    (root / ".cw/review-findings/code-reviewer.json").write_text(
        _document(_ROLE), encoding="utf-8"
    )
    reconcile_workspace(root)
    assert _marker(root)["completed_roles"] == [_ROLE]


def _orphan_staging(root: Path) -> None:
    """A staging round with no manifest is recovered from staging."""
    _write_staging_round(root, ticket="T-1", round_id="round-1")
    recover_workspace(root)


def _write_object(root: Path) -> None:
    _owned_callable("_write_object")(root / "probe.json", {}, "probe object")


def _enter_lock(root: Path) -> None:
    lock = _owned_callable("_workspace_lock")(root)
    assert isinstance(lock, contextlib.AbstractContextManager)
    with lock:
        pass


# Keyed by caller: a scenario that reaches that caller.
_PROBES: dict[str, Callable[[Path], None]] = {
    "_write_object": _write_object,
    "record_document": _returns,
    "record_failure": _returns,
    "record_reprompt": _reprompted,
    "finish_workspace": _returns,
    "_start_round": _first_run,
    "_set_phase": _first_run,
    "_recover_no_old": _first_run,
    "_reconcile_marker": _reconcile_returned,
    "_recover_old_active_pair": _archived_round,
    "_recover_old_directory_moved": _archived_round,
    "_recover_old_archived": _archived_round,
    "_recover_old_new_directory": _archived_round,
    "_transaction_from_staging": _orphan_staging,
    "_workspace_lock": _enter_lock,
}


def test_every_caller_has_a_probe() -> None:
    assert set(_PROBES) == {caller for _, caller in _CALL_PAIRS}


@pytest.mark.parametrize(("name", "caller"), _CALL_PAIRS)
def test_a_patch_on_the_callers_module_reaches_the_caller(
    name: str, caller: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Patch *name* on the module ``_OWNER`` places *caller* in; see it called.

    The recorder delegates to the original, so the scenario still runs for
    real, and records the calling function's name.
    """
    owner = _owner(caller)
    module_attr, _, attr = name.rpartition(".")
    target = vars(owner)[module_attr] if module_attr else owner
    original = getattr(target, attr)
    seen: list[str] = []

    def recorder(*args: object, **kwargs: object) -> object:
        seen.append(inspect.stack(0)[1].function)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, attr, recorder)
    _PROBES[caller](tmp_path)
    assert caller in seen


# ---------------------------------------------------------------------------
# Final-state guards: module size, the pinned surface, a pure ``__init__``,
# and the modules each patched name is called from
# ---------------------------------------------------------------------------

#: CLAUDE.md's "Module Size" yellow flag; the flat module was 1432 lines.
_MODULE_LINE_LIMIT = 800

#: The package's ``__all__``: the 9 public names plus the 13 private names.
_ALL = frozenset(
    {
        "TransactionPhase",
        "WorkspaceResult",
        "_Transaction",
        "_change_marker",
        "_kind",
        "_new_marker",
        "_new_transaction",
        "_parse_marker",
        "_read_object",
        "_rename",
        "_verify_bundle",
        "_workspace_lock",
        "_write_marker",
        "_write_object",
        "_write_transaction",
        "finish_workspace",
        "initialize_workspace",
        "reconcile_workspace",
        "record_document",
        "record_failure",
        "record_reprompt",
        "recover_workspace",
    }
)

#: The submodules (by stem) that call each patched name. A test patching one
#: of these names must patch it on every module listed, or part of the
#: behavior it means to intercept runs unpatched.
_CALL_SITE_MODULES: dict[str, frozenset[str]] = {
    "_write_transaction": frozenset({"_recovery", "_rounds"}),
    "_rename": frozenset({"_recovery"}),
    "_write_marker": frozenset({"_marker", "_rounds"}),
    "atomic_write_text": frozenset({"_primitives", "_rounds"}),
    "fcntl.flock": frozenset({"_lock"}),
}


def test_the_flat_module_is_gone() -> None:
    """The package directory replaced the flat file."""
    init_file = _module_file(_PKG)
    assert init_file.name == "__init__.py"
    assert not (init_file.parent.parent / "review_workspace.py").exists()


@pytest.mark.parametrize("path", _package_files(_PKG), ids=lambda path: path.name)
def test_package_file_is_under_the_module_size_flag(path: Path) -> None:
    """No submodule grows back toward the flat module's size."""
    assert len(path.read_text(encoding="utf-8").splitlines()) < _MODULE_LINE_LIMIT


def test_package_all_is_the_pinned_surface() -> None:
    """``__all__`` lists exactly the 22 surface names, no more and no fewer."""
    package = importlib.import_module(_PKG)
    assert sorted(package.__all__) == sorted(_ALL)
    assert set(_PUBLIC) | set(_PRIVATE) == _ALL


def test_every_name_in_all_is_bound() -> None:
    package = importlib.import_module(_PKG)
    assert [name for name in package.__all__ if not hasattr(package, name)] == []


def test_package_init_is_a_pure_reexport() -> None:
    """``__init__`` binds only re-exports and its submodules.

    Guards against a silent re-merge: a body moved back into ``__init__``
    would add a definition or a bound name here.
    """
    tree = _parse(_module_file(_PKG))
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        )
    ]
    bound = {
        name
        for name in vars(importlib.import_module(_PKG))
        if not name.startswith("__")
    }
    submodules = {path.stem for path in _package_files(_PKG)} - {"__init__"}
    assert bound == set(_ALL) | submodules


def test_call_sites_sit_in_the_pinned_modules() -> None:
    """The derived call-site map matches the hardcoded final layout."""
    derived = {
        name: {module for _, module in pairs} for name, pairs in _call_sites().items()
    }
    assert derived == {
        name: {f"{_PKG}.{stem}" for stem in stems}
        for name, stems in _CALL_SITE_MODULES.items()
    }
