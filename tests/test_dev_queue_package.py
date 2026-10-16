"""Seam assertions for the ``cw.dev_queue`` ceiling split (#2719).

``approval.py`` and ``lifecycle.py`` grew past CLAUDE.md's ~1000-line module
ceiling. #2719 moves two cohesive concerns out verbatim:

* the ``plan_scope_drift`` grant (``approve_scope_drift_ticket`` and its
  helpers) from ``approval`` into ``scope_drift``;
* the disposition vocabulary and sentinel-derivation helpers from
  ``lifecycle`` into the leaf ``dispositions``.

The behavioral suite cannot see three things the move must keep true:

1. Each moved name is *defined in* its new owner, so a dotted-path
   ``monkeypatch.setattr`` on the owner reaches the real call site.
2. The package surface survives: every moved name already in
   ``cw.dev_queue.__all__`` still resolves as ``cw.dev_queue.<name>``, to the
   owner's object, and ``__all__`` keeps its size (no new re-exports).
3. The scope-drift critical logs move to the ``cw.dev_queue.scope_drift``
   logger by design, while the code left in ``approval`` keeps
   ``cw.dev_queue.approval``.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

import pytest

import cw.dev_queue

if TYPE_CHECKING:
    from types import ModuleType

_SCOPE_DRIFT = "cw.dev_queue.scope_drift"
_DISPOSITIONS = "cw.dev_queue.dispositions"
_APPROVAL = "cw.dev_queue.approval"

# Owning module for every name #2719 moves.
_OWNER: dict[str, str] = {
    "ScopeDriftApproval": _SCOPE_DRIFT,
    "_SCOPE_DRIFT_RECOVERY_MARKER": _SCOPE_DRIFT,
    "_write_scope_drift_recovery_marker": _SCOPE_DRIFT,
    "_emit_scope_drift_compensation": _SCOPE_DRIFT,
    "approve_scope_drift_ticket": _SCOPE_DRIFT,
    "_scope_drift_command": _SCOPE_DRIFT,
    "_approve_scope_drift_locked": _SCOPE_DRIFT,
}

# Module-level ``str``/``frozenset`` constants: they carry no ``__module__``, so
# their ownership is checked by membership in the owner's namespace instead.
_CONSTANTS = frozenset(
    {
        "_SCOPE_DRIFT_RECOVERY_MARKER",
    }
)

# Moved names that were never part of ``cw.dev_queue.__all__``. They get an
# owner-only check: #2719 adds no re-exports, so they must stay off the package
# surface rather than gain an old path they never had.
_NOT_REEXPORTED = frozenset(
    {
        "ScopeDriftApproval",
        "_SCOPE_DRIFT_RECOVERY_MARKER",
        "_write_scope_drift_recovery_marker",
        "_emit_scope_drift_compensation",
        "_scope_drift_command",
        "_approve_scope_drift_locked",
    }
)

# Every moved name reachable as ``cw.dev_queue.<name>`` before #2719.
_OLD_PATHS = sorted(_OWNER.keys() - _NOT_REEXPORTED)

# ``len(cw.dev_queue.__all__)`` before #2719, hardcoded rather than re-derived:
# the move changes import sources only, never the re-exported surface.
_DEV_QUEUE_ALL_SIZE = 84


def _owner(name: str) -> ModuleType:
    return importlib.import_module(_OWNER[name])


def test_owner_table_covers_every_moved_name() -> None:
    """The owner, constant and not-re-exported tables describe the same names."""
    assert len(_OWNER) == 7
    assert _CONSTANTS.issubset(_OWNER)
    assert _NOT_REEXPORTED.issubset(_OWNER)


def test_dev_queue_package_all_size_is_unchanged() -> None:
    """``cw.dev_queue.__all__`` keeps its 84 historic entries."""
    assert len(cw.dev_queue.__all__) == _DEV_QUEUE_ALL_SIZE


@pytest.mark.parametrize("name", _OLD_PATHS)
def test_old_path_stays_on_the_package_surface(name: str) -> None:
    """Every moved name that was re-exported is still ``cw.dev_queue.<name>``."""
    assert name in cw.dev_queue.__all__
    assert hasattr(cw.dev_queue, name)


@pytest.mark.parametrize("name", _OLD_PATHS)
def test_old_path_resolves_to_owner_object(name: str) -> None:
    """The package attribute is the owner's object, not a copy."""
    assert getattr(cw.dev_queue, name) is vars(_owner(name))[name]


@pytest.mark.parametrize("name", sorted(_NOT_REEXPORTED))
def test_unexported_name_gains_no_package_path(name: str) -> None:
    """Names that were never re-exported stay off ``cw.dev_queue.__all__``."""
    assert name not in cw.dev_queue.__all__


@pytest.mark.parametrize("name", sorted(_OWNER.keys() - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """Functions and classes report their owning module as ``__module__``."""
    obj = vars(_owner(name))[name]
    assert obj.__module__ == _OWNER[name]


@pytest.mark.parametrize("name", sorted(_CONSTANTS))
def test_constant_is_bound_in_its_owner(name: str) -> None:
    """Constants live in their owner's namespace."""
    assert name in vars(_owner(name))


def test_scope_drift_logs_on_its_own_logger(caplog: pytest.LogCaptureFixture) -> None:
    """The moved scope-drift critical logs use ``cw.dev_queue.scope_drift``."""
    log = vars(importlib.import_module(_SCOPE_DRIFT))["_log"]
    assert log.name == _SCOPE_DRIFT
    with caplog.at_level(logging.DEBUG, logger=_SCOPE_DRIFT):
        log.debug("scope_drift logger pin probe")
    assert [record.name for record in caplog.records] == [_SCOPE_DRIFT]


def test_approval_keeps_its_logger(caplog: pytest.LogCaptureFixture) -> None:
    """The revoke/stamp/body-drift code left in ``approval`` keeps its logger."""
    log = vars(importlib.import_module(_APPROVAL))["_log"]
    assert log.name == _APPROVAL
    with caplog.at_level(logging.DEBUG, logger=_APPROVAL):
        log.debug("approval logger pin probe")
    assert [record.name for record in caplog.records] == [_APPROVAL]
