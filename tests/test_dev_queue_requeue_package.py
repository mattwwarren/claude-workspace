"""Seam assertions for the ``cw.dev_queue.requeue`` package split (#3739).

The flat ``cw/dev_queue/requeue.py`` passed CLAUDE.md's ~1000-line module
ceiling. #3739 moves its code verbatim into six submodules (``plan_guard``,
``stage_gate``, ``review_delivery``, ``live_session``, ``core``, ``unblock``)
behind a re-exporting ``__init__``. The behavioral suite cannot see what the
move must keep true:

1. The surface survives: the 11-name ``__all__`` resolves, and the names
   ``cw.dev_queue`` and the private importers use are the owners' objects.
2. Each name is defined in its owning submodule, so a patch on the owner
   reaches the real call site. A patch string aimed at the package would
   silently do nothing, so the package must not carry those names.
3. The sibling import DAG is exactly ``stage_gate -> plan_guard`` and
   ``core -> {stage_gate, review_delivery, live_session}``.
4. The package stays split: ``__init__`` is a pure re-export, every file is under
   the size ceiling, and every submodule imports cold.
"""

from __future__ import annotations

import ast
import importlib
from typing import TYPE_CHECKING

import pytest

import cw.dev_queue
import cw.dev_queue.requeue
from tests._package_split_helpers import (
    _imported_modules,
    _module_name,
    _package_files,
    _package_root,
    _run_cold,
    _runtime_imports,
)

if TYPE_CHECKING:
    from types import ModuleType

_PKG = "cw.dev_queue.requeue"
_PKG_DIR = _package_root(_PKG)
_CEILING = 1000

# Owning submodule for every top-level name the flat module defined.
_OWNER: dict[str, str] = {
    "_impl_bypass_worktree_path": "plan_guard",
    "_ImplBypassPlanCheck": "plan_guard",
    "_impl_bypass_plan_available": "plan_guard",
    "is_pending_requeue_noop": "stage_gate",
    "_validate_requeue_stages": "stage_gate",
    "_classify_requeue_move": "stage_gate",
    "_forward_gate_applied": "stage_gate",
    "_apply_requeue_stage": "stage_gate",
    "_ReviewDeliverability": "review_delivery",
    "_review_reentry_deliverable": "review_delivery",
    "RequeueRefusalTag": "live_session",
    "_REASON_SKIPPED_LIVE_SESSION": "live_session",
    "_REASON_SKIPPED_ROSTER_UNREADABLE": "live_session",
    "_live_short_ids_or_refuse": "live_session",
    "_refuse_if_live_session": "live_session",
    "classify_requeue_live_session_error": "live_session",
    "precheck_requeue": "core",
    "requeue_ticket": "core",
    "unblock_ticket": "unblock",
}

# The package ``__all__``: the public names plus the private names with importers.
_SURFACE = {
    "RequeueRefusalTag",
    "classify_requeue_live_session_error",
    "is_pending_requeue_noop",
    "precheck_requeue",
    "requeue_ticket",
    "unblock_ticket",
    "_ImplBypassPlanCheck",
    "_apply_requeue_stage",
    "_impl_bypass_plan_available",
    "_impl_bypass_worktree_path",
    "_refuse_if_live_session",
}

# Names with no ``__module__`` of their own (a ``Literal`` alias and two strings):
# ownership is namespace membership only.
_NO_MODULE_ATTR = {
    "RequeueRefusalTag",
    "_REASON_SKIPPED_LIVE_SESSION",
    "_REASON_SKIPPED_ROSTER_UNREADABLE",
}

# The names ``cw.dev_queue`` re-exports from the requeue package (its __init__).
_CW_DEV_QUEUE_NAMES = (
    "_apply_requeue_stage",
    "_impl_bypass_plan_available",
    "classify_requeue_live_session_error",
    "is_pending_requeue_noop",
    "precheck_requeue",
    "requeue_ticket",
    "unblock_ticket",
)

# Every (owning submodule, name) pair a patch string or module-object patch
# targets (#3739 retargets). Each name must live in its submodule's namespace.
_PATCH_SEAMS: dict[str, tuple[str, ...]] = {
    "plan_guard": (
        "fetch_approved_plan_comment",
        "worktree_path_for",
        "_checked_out_branch",
        "resolve_tracker",
        "_plan_body_signoff_ok",
    ),
    "core": (
        "record_event",
        "get_native_daemon_client",
        "_lock",
        "_refuse_if_live_session",
    ),
    "stage_gate": ("resolve_task_worktree", "merge_in_progress"),
    "unblock": ("load_dev_queue", "refusal_suffix"),
}

# Names the package no longer carries: a stale patch string aimed here would
# resolve to nothing and silently patch nothing, so the package must not have them.
_LOUD_MISSING = (
    "fetch_approved_plan_comment",
    "worktree_path_for",
    "_checked_out_branch",
    "_plan_body_signoff_ok",
    "resolve_tracker",
    "record_event",
    "get_native_daemon_client",
    "_lock",
    "merge_in_progress",
    "resolve_task_worktree",
    "load_dev_queue",
    "refusal_suffix",
)

_SUBMODULES = sorted(set(_OWNER.values()))

# The sibling import DAG, derived from runtime (module-level) imports.
_EXPECTED_EDGES: dict[str, set[str]] = {
    "plan_guard": set(),
    "stage_gate": {"plan_guard"},
    "review_delivery": set(),
    "live_session": set(),
    "core": {"stage_gate", "review_delivery", "live_session"},
    "unblock": set(),
}


def _sub(name: str) -> ModuleType:
    return importlib.import_module(f"{_PKG}.{name}")


def test_surface_is_the_eleven_name_all() -> None:
    assert set(cw.dev_queue.requeue.__all__) == _SURFACE


@pytest.mark.parametrize("name", sorted(_SURFACE))
def test_package_name_is_owner_object(name: str) -> None:
    owner = _sub(_OWNER[name])
    assert getattr(cw.dev_queue.requeue, name) is getattr(owner, name)


@pytest.mark.parametrize("name", sorted(_OWNER))
def test_owner_binds_the_name(name: str) -> None:
    assert name in vars(_sub(_OWNER[name]))


@pytest.mark.parametrize("name", sorted(set(_OWNER) - _NO_MODULE_ATTR))
def test_owner_defines_the_object(name: str) -> None:
    """An importer binds the name too; only the owner is its ``__module__``."""
    obj = vars(_sub(_OWNER[name]))[name]
    assert obj.__module__ == f"{_PKG}.{_OWNER[name]}"


@pytest.mark.parametrize("name", _CW_DEV_QUEUE_NAMES)
def test_cw_dev_queue_reexports_are_the_same_objects(name: str) -> None:
    assert getattr(cw.dev_queue, name) is getattr(cw.dev_queue.requeue, name)


def test_requeue_is_a_package_not_a_shadowed_module() -> None:
    assert hasattr(cw.dev_queue.requeue, "__path__")


@pytest.mark.parametrize(
    ("sub", "name"),
    [(sub, name) for sub, names in _PATCH_SEAMS.items() for name in names],
)
def test_patch_seam_lives_in_its_submodule(sub: str, name: str) -> None:
    assert name in vars(_sub(sub))


@pytest.mark.parametrize("name", _LOUD_MISSING)
def test_package_lacks_stale_patch_targets(name: str) -> None:
    assert not hasattr(cw.dev_queue.requeue, name)


def test_init_is_a_pure_reexport() -> None:
    tree = ast.parse((_PKG_DIR / "__init__.py").read_text())
    for node in tree.body:
        assert not isinstance(node, (ast.ClassDef, ast.FunctionDef)), node
    bound = {name for name in vars(cw.dev_queue.requeue) if not name.startswith("__")}
    # ``annotations`` is the ``from __future__ import annotations`` binding.
    assert bound == _SURFACE | set(_SUBMODULES) | {"annotations"}


def test_sibling_edges_are_exactly_the_dag() -> None:
    edges: dict[str, set[str]] = {}
    for path in _package_files(_PKG):
        if path.name == "__init__.py":
            continue
        mod = _module_name(path, _PKG).rsplit(".", 1)[-1]
        tree = ast.parse(path.read_text())
        siblings = {
            m.rsplit(".", 1)[-1]
            for node in _runtime_imports(tree.body)
            for m in _imported_modules(node)
            if m.startswith(f"{_PKG}.")
        }
        edges[mod] = siblings
    assert edges == _EXPECTED_EDGES


def test_no_submodule_imports_the_bare_package() -> None:
    for path in _package_files(_PKG):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == _PKG:
                pytest.fail(f"{path.name} imports the bare package")


def test_every_package_file_under_ceiling() -> None:
    for path in _package_files(_PKG):
        lines = len(path.read_text().splitlines())
        assert lines < _CEILING, f"{path.name}: {lines} lines"


@pytest.mark.parametrize("sub", _SUBMODULES)
def test_each_submodule_imports_cold(sub: str) -> None:
    _run_cold(f"import {_PKG}.{sub}")


def test_cold_import_of_package() -> None:
    _run_cold(f"import {_PKG}")


def test_module_names_match_files() -> None:
    names = {_module_name(p, _PKG) for p in _package_files(_PKG)}
    assert names == {_PKG} | {f"{_PKG}.{s}" for s in _SUBMODULES}
