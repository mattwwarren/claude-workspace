"""Seam assertions for the ``cw.dispatch.gating`` package split (#2503).

``gating`` was a single 1038-line flat module holding six preflight gate
families. The split must keep three things true that the behavioral suite
cannot see on its own:

1. The historic import surface survives: every one of the 34 top-level names
   stays an attribute of ``cw.dispatch.gating``, and the 21 that
   ``cw.dispatch`` re-exports stay attributes of ``cw.dispatch``.
2. Each name is *defined in* the module the owner table names, and every
   patched free name is looked up from the module its caller was defined in
   (``caller.__globals__``). A ``monkeypatch.setattr`` on a dotted path only
   reaches the real call site when it targets that module, so a stale patch
   target would otherwise go silently inert.
3. Every logging module keeps emitting on the ``cw.dispatch`` logger, so
   ``caplog`` filters and operator log routing see the same records.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

import cw.dispatch
import cw.dispatch.gating

if TYPE_CHECKING:
    from types import ModuleType

_GATING = "cw.dispatch.gating"
_CONTEXT_JSON = f"{_GATING}.context_json"
_USAGE_LIMIT = f"{_GATING}.usage_limit"
_AVAILABILITY = f"{_GATING}.availability"
_SSH_KEY = f"{_GATING}.ssh_key"
_FRESHNESS = f"{_GATING}.freshness"

# Owning module for each of the 34 historic top-level names of the flat
# ``gating.py``. Each extraction commit of the split edits only the entries it
# moves.
_OWNER: dict[str, str] = {
    "_emit_usage_limit_skip_events": _USAGE_LIMIT,
    "_reconcile_usage_limited": _USAGE_LIMIT,
    "_AVAILABILITY_OUTAGE_REASON": _AVAILABILITY,
    "_AVAILABILITY_PROBE_TIMEOUT_SECONDS": _AVAILABILITY,
    "_AVAILABILITY_PROBE_TTL_SECONDS": _AVAILABILITY,
    "_resolve_availability": _AVAILABILITY,
    "_resolve_availability_once": _AVAILABILITY,
    "_record_availability_block": _AVAILABILITY,
    "_reset_availability_block": _AVAILABILITY,
    "_emit_availability_skip": _AVAILABILITY,
    "FRESHNESS_NON_MAIN_HEAD": _FRESHNESS,
    "FRESHNESS_MAIN_BEHIND": _FRESHNESS,
    "FRESHNESS_MAIN_DIRTY_CHECKOUT": _FRESHNESS,
    "FRESHNESS_MAIN_DIVERGED": _FRESHNESS,
    "FRESHNESS_MAIN_DETACHED": _FRESHNESS,
    "_resolve_freshness": _FRESHNESS,
    "_emit_stale_skip": _FRESHNESS,
    "_SSH_KEY_WARN_SENTINEL": _SSH_KEY,
    "_resolve_ssh_key_once": _SSH_KEY,
    "_emit_ssh_key_skip": _SSH_KEY,
    "_emit_ssh_key_bypass": _SSH_KEY,
    "_apply_ssh_key_gate": _SSH_KEY,
    "_HOST_TMP_EXHAUSTED_REASON": _GATING,
    "_DiskPressure": _GATING,
    "_resolve_inode_pressure": _GATING,
    "_resolve_disk_pressure": _GATING,
    "_disk_pressure_warn_line": _GATING,
    "_emit_disk_pressure_skip": _GATING,
    "_emit_disk_pressure_bypass": _GATING,
    "_record_host_tmp_exhausted_block": _GATING,
    "_reset_host_tmp_exhausted_block": _GATING,
    "_update_host_tmp_latch": _GATING,
    "_apply_disk_pressure_gate": _GATING,
    "_invalidate_stale_context_json": _CONTEXT_JSON,
}

# Module-level ``str``/``int`` constants: they carry no ``__module__``, so their
# ownership is checked by membership in the owner's namespace instead.
_CONSTANTS = frozenset(
    {
        "_AVAILABILITY_OUTAGE_REASON",
        "_AVAILABILITY_PROBE_TIMEOUT_SECONDS",
        "_AVAILABILITY_PROBE_TTL_SECONDS",
        "_HOST_TMP_EXHAUSTED_REASON",
        "FRESHNESS_NON_MAIN_HEAD",
        "FRESHNESS_MAIN_BEHIND",
        "FRESHNESS_MAIN_DIRTY_CHECKOUT",
        "FRESHNESS_MAIN_DIVERGED",
        "FRESHNESS_MAIN_DETACHED",
        "_SSH_KEY_WARN_SENTINEL",
    }
)

# The subset of the historic surface that ``cw.dispatch`` re-exports
# (dispatch/__init__.py's ``from cw.dispatch.gating import (...)`` block).
_DISPATCH_REEXPORTS = (
    "_AVAILABILITY_OUTAGE_REASON",
    "_AVAILABILITY_PROBE_TIMEOUT_SECONDS",
    "_AVAILABILITY_PROBE_TTL_SECONDS",
    "_SSH_KEY_WARN_SENTINEL",
    "FRESHNESS_MAIN_BEHIND",
    "FRESHNESS_MAIN_DETACHED",
    "FRESHNESS_MAIN_DIRTY_CHECKOUT",
    "FRESHNESS_MAIN_DIVERGED",
    "FRESHNESS_NON_MAIN_HEAD",
    "_emit_availability_skip",
    "_emit_ssh_key_skip",
    "_emit_stale_skip",
    "_emit_usage_limit_skip_events",
    "_invalidate_stale_context_json",
    "_reconcile_usage_limited",
    "_record_availability_block",
    "_reset_availability_block",
    "_resolve_availability",
    "_resolve_availability_once",
    "_resolve_freshness",
    "_resolve_ssh_key_once",
)

# (patched free name, caller that looks it up). The seam's owner is the
# caller's owner: tests monkeypatch ``<owner>.<free name>``, which only
# reaches the call site when the caller's globals ARE the owner's namespace.
_SEAMS = (
    ("reconcile", "_reconcile_usage_limited"),
    ("is_main_behind_origin", "_resolve_freshness"),
    ("check_main_ff_safety", "_resolve_freshness"),
    ("fast_forward_main", "_resolve_freshness"),
    ("get_head_branch", "_resolve_freshness"),
    ("is_main_checkout_dirty", "_resolve_freshness"),
    ("get_head_branch", "_emit_stale_skip"),
    ("check_gh_availability", "_resolve_availability"),
    ("check_ssh_key_available", "_resolve_ssh_key_once"),
    ("push_remote_scheme", "_apply_ssh_key_gate"),
    ("check_disk_usage", "_resolve_disk_pressure"),
    ("check_inode_usage", "_resolve_inode_pressure"),
)

# Every module that defines a ``_log``; each must log on ``cw.dispatch``.
_LOGGING_MODULES = (
    _GATING,
    _CONTEXT_JSON,
    _USAGE_LIMIT,
    _AVAILABILITY,
    _SSH_KEY,
    _FRESHNESS,
)

# Every extracted gating submodule; each must import cold in a fresh interpreter.
_SUBMODULES = ("context_json", "usage_limit", "availability", "ssh_key", "freshness")


def _owner(name: str) -> ModuleType:
    return importlib.import_module(_OWNER[name])


def test_owner_table_covers_the_historic_surface() -> None:
    """The owner table names exactly the 34 historic top-level names."""
    assert len(_OWNER) == 34
    assert _CONSTANTS.issubset(_OWNER)
    assert set(_DISPATCH_REEXPORTS).issubset(_OWNER)


@pytest.mark.parametrize("name", sorted(_OWNER))
def test_gating_package_keeps_historic_name(name: str) -> None:
    """Every historic name stays an attribute of ``cw.dispatch.gating``."""
    assert hasattr(cw.dispatch.gating, name)


@pytest.mark.parametrize("name", _DISPATCH_REEXPORTS)
def test_dispatch_package_keeps_gating_reexport(name: str) -> None:
    """``from cw.dispatch import X`` keeps working for every gating re-export."""
    assert getattr(cw.dispatch, name) is getattr(cw.dispatch.gating, name)


@pytest.mark.parametrize("name", sorted(_OWNER.keys() - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """Functions and classes report their owning module as ``__module__``."""
    obj = getattr(cw.dispatch.gating, name)
    assert obj.__module__ == _OWNER[name]
    assert vars(_owner(name))[name] is obj


@pytest.mark.parametrize("name", sorted(_CONSTANTS))
def test_constant_is_bound_in_its_owner(name: str) -> None:
    """Constants live in their owner's namespace with the re-exported value."""
    owner_namespace = vars(_owner(name))
    assert name in owner_namespace
    assert getattr(cw.dispatch.gating, name) == owner_namespace[name]


@pytest.mark.parametrize(("free_name", "caller_name"), _SEAMS)
def test_patched_seam_resolves_in_callers_owner(
    free_name: str, caller_name: str
) -> None:
    """A dotted-path patch on the owner reaches the caller's free-name lookup."""
    owner = _owner(caller_name)
    caller = vars(owner)[caller_name]
    assert free_name in vars(owner)
    assert caller.__globals__ is vars(owner)


@pytest.mark.parametrize("module_name", _LOGGING_MODULES)
def test_module_logs_on_cw_dispatch(
    module_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Records emitted through each module's ``_log`` carry ``cw.dispatch``."""
    log = vars(importlib.import_module(module_name))["_log"]
    assert log.name == "cw.dispatch"
    with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
        log.debug("gating logger pin probe")
    assert [record.name for record in caplog.records] == ["cw.dispatch"]


def test_logger_name_is_defined_once_and_kept_out_of_all() -> None:
    """``_LOGGER_NAME`` lives in ``context_json``; the package only re-exports it."""
    context_json = importlib.import_module(_CONTEXT_JSON)
    assert vars(context_json)["_LOGGER_NAME"] == "cw.dispatch"
    assert vars(cw.dispatch.gating)["_LOGGER_NAME"] == "cw.dispatch"
    assert "_LOGGER_NAME" not in cw.dispatch.gating.__all__


def test_package_all_is_the_historic_surface() -> None:
    """``__all__`` lists exactly the 34 historic names."""
    assert sorted(cw.dispatch.gating.__all__) == sorted(_OWNER)


@pytest.mark.parametrize("submodule", _SUBMODULES)
def test_submodule_imports_cold(submodule: str) -> None:
    """Each submodule imports in a fresh isolated interpreter (no cycle)."""
    subprocess.run(
        [sys.executable, "-I", "-c", f"import {_GATING}.{submodule}"],
        check=True,
        env=os.environ.copy(),
    )
