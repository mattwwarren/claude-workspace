"""Seam assertions for the ``cw.reconcile.concierge`` package split (#2895).

The flat ``cw/reconcile/concierge.py`` passed CLAUDE.md's ~1000-line module
ceiling. #2895 moves its code verbatim into five submodules (``_common``,
``false_park``, ``park_marker_poison``, ``cancelled_row``, ``core``) behind a
re-exporting ``__init__``.

The behavioral suite cannot see four things the move must keep true:

1. The surface survives: every name imported from ``cw.reconcile.concierge``
   before the split still resolves there and is listed in ``__all__``.
2. Each name is *defined in* its owning submodule, and the package attribute
   is that object, so a patch on the owner reaches the real call site.
3. The patch seams live where the tests patch them: ``_transcript_age_seconds``
   is read by both ``false_park`` and ``park_marker_poison``, and each other
   seam is bound in the module whose function looks it up. A patch on the
   package re-export silently stops intercepting.
4. The package stays split: every submodule is under the size flag and
   ``__init__`` is a pure re-export.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw.reconcile.concierge

if TYPE_CHECKING:
    from types import ModuleType

_COMMON = "cw.reconcile.concierge._common"
_FALSE_PARK = "cw.reconcile.concierge.false_park"
_PARK_MARKER_POISON = "cw.reconcile.concierge.park_marker_poison"
_CANCELLED_ROW = "cw.reconcile.concierge.cancelled_row"
_CORE = "cw.reconcile.concierge.core"

_PACKAGE_DIR = Path(cw.reconcile.concierge.__file__).parent

# Owning submodule for every name on the package surface. Hardcoded, not
# derived from the package, so a name that drifts to another module fails.
_OWNER: dict[str, str] = {
    "DEFAULT_CONCIERGE_RECOVERIES": _COMMON,
    "RECIPE_CANCELLED_ROW_RESTORE": _COMMON,
    "RECIPE_FALSE_PARK_REQUEUE": _COMMON,
    "RECIPE_PARK_MARKER_POISON_CLEAR": _COMMON,
    "ConciergeCandidate": _COMMON,
    "resolve_concierge_recipe_enabled": _COMMON,
    "_FALSE_PARK_ELIGIBLE_DISPOSITIONS": _FALSE_PARK,
    "_detect_false_park_candidates": _FALSE_PARK,
    "_resolve_false_park_recovery_backoff": _FALSE_PARK,
    "_act_on_park_marker_poison_candidates": _PARK_MARKER_POISON,
    "_close_confirmed_dead_session": _PARK_MARKER_POISON,
    "_park_marker_transcript_stale_45m": _PARK_MARKER_POISON,
    "_act_on_cancelled_row_candidates": _CANCELLED_ROW,
    "run_concierge_recoveries": _CORE,
}

# The package surface: the public names plus the private ones tests import.
_SURFACE = frozenset(_OWNER)

# Constants carry no ``__module__``; ownership is checked by membership in the
# owner's namespace instead. ``_FALSE_PARK_ELIGIBLE_DISPOSITIONS`` is an alias
# of a ``cw.reconcile._shared`` frozenset, bound in ``false_park``.
_CONSTANTS = frozenset(
    {
        "DEFAULT_CONCIERGE_RECOVERIES",
        "RECIPE_CANCELLED_ROW_RESTORE",
        "RECIPE_FALSE_PARK_REQUEUE",
        "RECIPE_PARK_MARKER_POISON_CLEAR",
        "_FALSE_PARK_ELIGIBLE_DISPOSITIONS",
    }
)

# (patched name, consumer module that must bind it, reader functions in that
# consumer whose globals the patch has to reach). Tests patch the name on the
# consumer, never on the package.
_SEAMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("_transcript_age_seconds", _FALSE_PARK, ("_detect_false_park_candidates",)),
    (
        "_transcript_age_seconds",
        _PARK_MARKER_POISON,
        ("_park_marker_transcript_stale_45m",),
    ),
    ("save_dev_queue", _FALSE_PARK, ("_act_on_false_park_candidates",)),
    (
        "salvage_terminal_result",
        _PARK_MARKER_POISON,
        ("_close_confirmed_dead_session",),
    ),
    (
        "_close_confirmed_dead_session",
        _PARK_MARKER_POISON,
        ("_act_on_park_marker_poison_candidates",),
    ),
    ("_detect_cancelled_row_candidates", _CORE, ("run_concierge_recoveries",)),
)

#: CLAUDE.md's "Module Size" yellow flag; the flat module was 1026 lines.
_MODULE_LINE_LIMIT = 800


def _owner(name: str) -> ModuleType:
    return importlib.import_module(_OWNER[name])


def _package_files() -> list[Path]:
    return sorted(_PACKAGE_DIR.glob("*.py")) if _PACKAGE_DIR.name == "concierge" else []


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_owner_table_covers_constants() -> None:
    """The owner and constant tables describe the same names."""
    assert len(_OWNER) == 14
    assert _CONSTANTS.issubset(_OWNER)


@pytest.mark.parametrize("name", sorted(_SURFACE))
def test_surface_name_resolves_on_the_package(name: str) -> None:
    """Every pre-split import is still reachable as ``cw.reconcile.concierge.X``."""
    assert hasattr(cw.reconcile.concierge, name)


def test_package_all_is_the_pinned_surface() -> None:
    """``__all__`` lists exactly the surface names, no more and no fewer."""
    assert sorted(getattr(cw.reconcile.concierge, "__all__", ())) == sorted(_SURFACE)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_SURFACE))
def test_package_attribute_is_the_owner_object(name: str) -> None:
    """The package attribute is the owner's object, not a copy."""
    assert getattr(cw.reconcile.concierge, name) is vars(_owner(name))[name]


@pytest.mark.parametrize("name", sorted(_SURFACE - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """Functions and classes report their owning submodule as ``__module__``."""
    obj = vars(_owner(name))[name]
    assert obj.__module__ == _OWNER[name]


@pytest.mark.parametrize("name", sorted(_CONSTANTS))
def test_constant_is_bound_in_its_owner(name: str) -> None:
    """Constants live in their owner's namespace."""
    assert name in vars(_owner(name))


@pytest.mark.parametrize(
    ("seam", "consumer", "readers"),
    _SEAMS,
    ids=[f"{seam}@{consumer.rsplit('.', 1)[-1]}" for seam, consumer, _ in _SEAMS],
)
def test_patch_seam_is_bound_where_it_is_read(
    seam: str, consumer: str, readers: tuple[str, ...]
) -> None:
    """The consumer binds the seam and its readers look it up there."""
    module = importlib.import_module(consumer)
    assert seam in vars(module)
    for reader in readers:
        fn = vars(module)[reader]
        assert inspect.isfunction(fn)
        assert fn.__globals__ is vars(module)
        assert seam in fn.__code__.co_names


def test_transcript_age_is_not_bound_on_the_package() -> None:
    """A stale ``cw.reconcile.concierge._transcript_age_seconds`` patch fails.

    The name is not re-exported, so ``monkeypatch.setattr`` on the package
    raises instead of silently patching an attribute no reader consults.
    """
    assert "_transcript_age_seconds" not in vars(cw.reconcile.concierge)


# ---------------------------------------------------------------------------
# Final-state guards: module size and a pure ``__init__``
# ---------------------------------------------------------------------------


def test_concierge_py_removed() -> None:
    """``cw.reconcile.concierge`` resolves to the package, not a flat module."""
    assert cw.reconcile.concierge.__file__ is not None
    assert cw.reconcile.concierge.__file__.endswith("__init__.py")
    assert not (_PACKAGE_DIR.parent / "concierge.py").exists()


def test_package_files_are_under_the_module_size_flag() -> None:
    """``cw.reconcile.concierge`` is a package and no submodule nears the ceiling."""
    files = _package_files()
    assert files, "cw.reconcile.concierge is not a package"
    for path in files:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count < _MODULE_LINE_LIMIT, path.name


def test_package_init_is_a_pure_reexport() -> None:
    """``__init__`` binds only re-exports, its submodules and ``annotations``."""
    files = _package_files()
    assert files, "cw.reconcile.concierge is not a package"
    tree = ast.parse((_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8"))
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        )
    ]
    bound = {name for name in vars(cw.reconcile.concierge) if not name.startswith("__")}
    submodules = {path.stem for path in files} - {"__init__"}
    # ``annotations`` is the ``from __future__ import annotations`` binding.
    assert bound == set(_SURFACE) | submodules | {"annotations"}
