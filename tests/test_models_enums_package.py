"""Seam assertions for the ``cw.models.enums`` package split (#3739).

The flat ``cw/models/enums.py`` passed CLAUDE.md's ~1000-line module ceiling.
#3739 moves its code verbatim into concern submodules behind a re-exporting
``__init__``. The behavioral suite cannot see what the move must keep true:

1. The surface survives: all 27 names resolve on ``cw.models.enums`` and are
   listed in ``__all__``, and the names ``cw.models`` re-exports are the same
   objects.
2. Each name is defined in its owning submodule, and ``__module__`` reports it.
3. The package stays a DAG root: no submodule imports ``cw`` at runtime, the
   ``__init__`` is a pure re-export, and every file is under the size ceiling.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import cw.models
import cw.models.enums
from tests._package_split_helpers import (
    _imported_modules,
    _is_cw,
    _module_name,
    _package_files,
    _package_root,
    _run_cold,
    _runtime_imports,
)

_PKG = "cw.models.enums"
_PKG_DIR = _package_root(_PKG)

# Owning submodule for every name on the package surface.
_OWNER: dict[str, str] = {
    "SessionPurpose": "session",
    "WORKER_PURPOSES": "session",
    "SessionStatus": "session",
    "TERMINAL_SESSION_STATUSES": "session",
    "CompletionReason": "session",
    "SessionOrigin": "session",
    "LastResultSource": "session",
    "LivenessBucket": "session",
    "QueueItemStatus": "queue_status",
    "OCCUPIED_LANE_STATUSES": "queue_status",
    "DEPENDENCY_HOLD_DISPOSITION": "queue_status",
    "WATCHED_PR_SOURCE_DEPENDENCY_HOLD": "queue_status",
    "TERMINAL_QUEUE_STATUSES": "queue_status",
    "ReapReason": "reap",
    "ReapPolicy": "reap",
    "Stage": "pipeline",
    "PipelinePosition": "pipeline",
    "stage_position": "pipeline",
    "StageIdentifier": "pipeline",
    "STAGE_IDENTIFIERS": "pipeline",
    "OrchestratorEventType": "event_types",
    "DispatchSkipReason": "dispatch",
    "CodexHarvestOutcome": "codex",
    "ReasoningEffort": "codex",
    "SandboxMode": "codex",
    "DEFAULT_REVIEWER_SANDBOX_MODE": "codex",
    "DEFAULT_FIX_SANDBOX_MODE": "codex",
}

# The names ``cw.models`` re-exports from ``cw.models.enums`` (its __init__).
_CW_MODELS_NAMES = (
    "OCCUPIED_LANE_STATUSES",
    "TERMINAL_QUEUE_STATUSES",
    "TERMINAL_SESSION_STATUSES",
    "WORKER_PURPOSES",
    "CodexHarvestOutcome",
    "CompletionReason",
    "DispatchSkipReason",
    "LastResultSource",
    "LivenessBucket",
    "OrchestratorEventType",
    "QueueItemStatus",
    "ReapPolicy",
    "ReapReason",
    "ReasoningEffort",
    "SandboxMode",
    "SessionOrigin",
    "SessionPurpose",
    "SessionStatus",
    "Stage",
)

_SUBMODULES = sorted(set(_OWNER.values()))
_CEILING = 1000


def _owner_module(name: str) -> object:
    return importlib_import(f"{_PKG}.{_OWNER[name]}")


def importlib_import(dotted: str) -> object:
    import importlib

    return importlib.import_module(dotted)


def test_surface_is_exactly_the_27_owned_names() -> None:
    assert set(cw.models.enums.__all__) == set(_OWNER)
    for name in _OWNER:
        assert hasattr(cw.models.enums, name), name


@pytest.mark.parametrize("name", sorted(_OWNER))
def test_package_attribute_is_owner_object(name: str) -> None:
    owner = _owner_module(name)
    assert getattr(cw.models.enums, name) is getattr(owner, name)


@pytest.mark.parametrize("name", sorted(_OWNER))
def test_owner_defines_the_object(name: str) -> None:
    owner = _owner_module(name)
    assert name in vars(owner)


def test_classes_report_their_owner_as_module() -> None:
    for name, sub in _OWNER.items():
        obj = getattr(cw.models.enums, name)
        if isinstance(obj, type) or name == "stage_position":
            assert obj.__module__ == f"{_PKG}.{sub}", name


def test_cw_models_reexports_are_the_same_objects() -> None:
    for name in _CW_MODELS_NAMES:
        assert getattr(cw.models, name) is getattr(cw.models.enums, name), name


def test_init_is_a_pure_reexport() -> None:
    tree = ast.parse((_PKG_DIR / "__init__.py").read_text())
    for node in tree.body:
        assert not isinstance(node, (ast.ClassDef, ast.FunctionDef)), node
    bound = {name for name in vars(cw.models.enums) if not name.startswith("__")}
    # ``annotations`` is the ``from __future__ import annotations`` binding.
    assert bound == set(_OWNER) | set(_SUBMODULES) | {"annotations"}


def test_submodules_are_dag_roots() -> None:
    for path in _package_files(_PKG):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text())
        for node in _runtime_imports(tree.body):
            for mod in _imported_modules(node):
                assert not _is_cw(mod), f"{path.name} imports {mod} at runtime"


@pytest.mark.parametrize("sub", _SUBMODULES)
def test_each_submodule_imports_cold(sub: str) -> None:
    _run_cold(f"import {_PKG}.{sub}")


def test_cold_import_of_package() -> None:
    _run_cold(f"import {_PKG}")


def test_every_package_file_under_ceiling() -> None:
    for path in _package_files(_PKG):
        lines = len(Path(path).read_text().splitlines())
        assert lines < _CEILING, f"{path.name}: {lines} lines"


def test_module_names_match_files() -> None:
    names = {_module_name(p, _PKG) for p in _package_files(_PKG)}
    assert names == {_PKG} | {f"{_PKG}.{s}" for s in _SUBMODULES}
