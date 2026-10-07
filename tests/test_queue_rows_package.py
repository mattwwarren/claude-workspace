"""Seam assertions for the ``cw.queue_rows`` / ``cw.claim_evidence`` leaves (#2613).

``cw.reconcile`` reached into ``cw.dispatch`` for a handful of pure queue-row
helpers through function-level deferred imports, because ``cw.dispatch``'s
package ``__init__`` imports ``cw.reconcile`` at module top. #2613 moves those
helpers byte-identically into two leaf modules that import nothing from either
package, so reconcile can import them at module scope. The move must keep four
things true that the behavioral suite cannot see on its own:

1. The historic import surface survives: every old dotted path
   (``cw.dispatch.claim.X``, ``cw.dispatch.X``, ``cw.dispatch.routing.X``,
   ``cw.dispatch.productivity.X``) still resolves, to the owner's object.
2. Each name is *defined in* the module the owner table names, and every
   patched free name is looked up from the module its caller was defined in
   (``caller.__globals__``), so a stale ``monkeypatch.setattr`` target fails
   here instead of going silently inert.
3. No logger moves: ``claimed_row`` keeps logging on ``cw.dispatch`` and the
   leaves bind no ``_log``.
4. The leaves really are leaves: a cold import of each one loads no module of
   the ``cw.dispatch`` / ``cw.reconcile`` / ``cw.executor`` / ``cw.spawn``
   cycle.
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
import cw.dispatch.claim
from cw.dispatch.claim import claimed_row

if TYPE_CHECKING:
    from types import ModuleType

_CLAIMED_ROW = "cw.dispatch.claim.claimed_row"
_SCREENING = "cw.dispatch.claim.screening"
_REVIEW_GATES = "cw.dispatch.review_gates"
_PR_REFS = "cw.dispatch.routing.pr_refs"
_PRODUCTIVITY = "cw.dispatch.productivity"
_QUEUE_ROWS = "cw.queue_rows"

# Owning module for every name #2613 moves, plus ``_stamp_spawn_success``,
# which stays behind in ``claimed_row``. Each extraction commit edits only the
# entries it moves.
_OWNER: dict[str, str] = {
    "_SPAWN_ERROR_BACKOFF_INITIAL_SECONDS": _QUEUE_ROWS,
    "_SPAWN_ERROR_BACKOFF_CAP_SECONDS": _QUEUE_ROWS,
    "_find_running_row": _QUEUE_ROWS,
    "_revert_claimed_task_to_pending": _QUEUE_ROWS,
    "_park_running_task_blocked_on_user": _QUEUE_ROWS,
    "_apply_spawn_success_fields": _QUEUE_ROWS,
    "_stamp_spawn_success": _CLAIMED_ROW,
    "_is_fix_dispatch_held": _SCREENING,
    "_is_backstop_exempt": _SCREENING,
    "resolve_hold_finalize": _REVIEW_GATES,
    "_AUTOMERGE_NOT_ARMED_REASON": _PR_REFS,
    "_PRIOR_PIPELINE_PR_OPEN_REASON": _PR_REFS,
    "ClaimEvidence": _PRODUCTIVITY,
    "extract_claim_evidence": _PRODUCTIVITY,
    "is_unproductive": _PRODUCTIVITY,
}

# Module-level ``str``/``int`` constants: they carry no ``__module__``, so their
# ownership is checked by membership in the owner's namespace instead.
_CONSTANTS = frozenset(
    {
        "_SPAWN_ERROR_BACKOFF_INITIAL_SECONDS",
        "_SPAWN_ERROR_BACKOFF_CAP_SECONDS",
        "_AUTOMERGE_NOT_ARMED_REASON",
        "_PRIOR_PIPELINE_PR_OPEN_REASON",
    }
)

# Every historic dotted-path home of each name, other than its owner. Each must
# keep resolving to the owner's object. ``cw.dispatch.routing.pr_refs.<const>``
# and ``cw.dispatch.review_gates.resolve_hold_finalize`` are deliberately not
# listed: #2613 repoints their consumers to the leaf instead of re-exporting
# through them (an ``as X`` alias outside ``__init__`` fails PLC0414).
_OLD_PATHS: dict[str, tuple[str, ...]] = {
    "_SPAWN_ERROR_BACKOFF_INITIAL_SECONDS": ("cw.dispatch.claim", "cw.dispatch"),
    "_SPAWN_ERROR_BACKOFF_CAP_SECONDS": ("cw.dispatch.claim", "cw.dispatch"),
    "_find_running_row": ("cw.dispatch.claim",),
    "_revert_claimed_task_to_pending": ("cw.dispatch.claim", "cw.dispatch"),
    "_park_running_task_blocked_on_user": ("cw.dispatch.claim", "cw.dispatch"),
    "_apply_spawn_success_fields": ("cw.dispatch.claim",),
    "_stamp_spawn_success": ("cw.dispatch.claim",),
    "_is_fix_dispatch_held": ("cw.dispatch.claim",),
    "_is_backstop_exempt": ("cw.dispatch.claim",),
    "resolve_hold_finalize": ("cw.dispatch",),
    "_AUTOMERGE_NOT_ARMED_REASON": ("cw.dispatch.routing",),
    "_PRIOR_PIPELINE_PR_OPEN_REASON": ("cw.dispatch.routing",),
    "ClaimEvidence": (_PRODUCTIVITY,),
    "extract_claim_evidence": (_PRODUCTIVITY,),
    "is_unproductive": (_PRODUCTIVITY,),
}

# (patched free name, caller that looks it up). The seam's owner is the
# caller's owner: tests monkeypatch ``<owner>.<free name>``, which only
# reaches the call site when the caller's globals ARE the owner's namespace.
_SEAMS = (
    ("dev_queue_lock", "_park_running_task_blocked_on_user"),
    ("load_dev_queue", "_revert_claimed_task_to_pending"),
    ("record_event", "_park_running_task_blocked_on_user"),
    ("git_output", "_stamp_spawn_success"),
    ("load_dev_queue", "_stamp_spawn_success"),
    ("dev_queue_lock", "_stamp_spawn_success"),
)

# The leaf modules created so far. Each extraction commit that creates a leaf
# adds it here; every leaf must bind no logger and import cold without loading
# any module of the dispatch/reconcile cycle.
_LEAVES: tuple[str, ...] = (_QUEUE_ROWS,)

# Module prefixes a leaf's cold import must not load (the import cycle).
_CYCLE_EXACT = ("cw.dispatch", "cw.executor")
_CYCLE_PREFIXES = ("cw.dispatch.", "cw.reconcile", "cw.executor.", "cw.spawn")

# ``cw.dispatch.claim.__all__`` before #2613, hardcoded rather than re-derived:
# the move changes import sources only, never the re-exported surface.
_CLAIM_ALL = frozenset(
    {
        "_CLAIM_BACKOFF",
        "_CLAIM_CLAIMED",
        "_CLAIM_SKIPPED",
        "_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS",
        "_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD",
        "_CODEX_CAPABILITY_PROBE_TTL_SECONDS",
        "_OCCUPIED_DEFER_SECONDS",
        "_SPAWN_ERROR_BACKOFF_CAP_SECONDS",
        "_SPAWN_ERROR_BACKOFF_INITIAL_SECONDS",
        "_SpawnOutcome",
        "_apply_plan_bypass_if_available",
        "_apply_spawn_success_fields",
        "_cached_codex_capability_diagnosis",
        "_claim_next_pending",
        "_codex_capability_cache",
        "_codex_capability_gate",
        "_codex_capability_park_count",
        "_defer_genuinely_live_hook_conflict",
        "_defer_occupied_claim",
        "_emit_attempt_cap_attention_event",
        "_emit_attempt_cap_blocked_event",
        "_emit_stale_dispatch_attention_event",
        "_emit_stale_dispatch_blocked_event",
        "_emit_worktree_occupied_skip_event",
        "_find_running_row",
        "_handle_hook_context_conflict",
        "_is_backstop_exempt",
        "_is_fix_dispatch_held",
        "_is_stale_pr_gated",
        "_lane_occupants_for_client",
        "_lane_stats_for_client",
        "_park_running_task_blocked_on_user",
        "_park_stale_pr_task",
        "_raise_if_stale_tree_occupied",
        "_reset_codex_capability_cache",
        "_revert_claimed_task_to_pending",
        "_screen_and_claim",
        "_spawn_claimed_task",
        "_spawn_error_tick_fields",
        "_stamp_spawn_success",
        "resolve_occupied_ticket_ids",
    }
)

# ``len(cw.dispatch.__all__)`` before #2613.
_DISPATCH_ALL_SIZE = 120


def _owner(name: str) -> ModuleType:
    return importlib.import_module(_OWNER[name])


def test_owner_table_covers_every_moved_name() -> None:
    """The owner, old-path and constant tables describe the same names."""
    assert len(_OWNER) == 15
    assert set(_OLD_PATHS) == set(_OWNER)
    assert _CONSTANTS.issubset(_OWNER)
    assert {caller for _, caller in _SEAMS}.issubset(_OWNER)
    assert set(_LEAVES).issubset(_OWNER.values())


def test_claim_package_all_is_unchanged() -> None:
    """``cw.dispatch.claim.__all__`` keeps exactly its 41 historic names."""
    assert len(_CLAIM_ALL) == 41
    assert set(cw.dispatch.claim.__all__) == _CLAIM_ALL


def test_dispatch_package_all_size_is_unchanged() -> None:
    """``cw.dispatch.__all__`` keeps its 120 historic entries."""
    assert len(cw.dispatch.__all__) == _DISPATCH_ALL_SIZE


@pytest.mark.parametrize(
    ("name", "path"),
    [(name, path) for name, paths in sorted(_OLD_PATHS.items()) for path in paths],
)
def test_old_path_resolves_to_owner_object(name: str, path: str) -> None:
    """Every historic dotted path still resolves, to the owner's object."""
    old_home = importlib.import_module(path)
    assert getattr(old_home, name) is vars(_owner(name))[name]


@pytest.mark.parametrize("name", sorted(_OWNER.keys() - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """Functions and classes report their owning module as ``__module__``."""
    obj = vars(_owner(name))[name]
    assert obj.__module__ == _OWNER[name]


@pytest.mark.parametrize("name", sorted(_CONSTANTS))
def test_constant_is_bound_in_its_owner(name: str) -> None:
    """Constants live in their owner's namespace."""
    assert name in vars(_owner(name))


@pytest.mark.parametrize(("free_name", "caller_name"), _SEAMS)
def test_patched_seam_resolves_in_callers_owner(
    free_name: str, caller_name: str
) -> None:
    """A dotted-path patch on the owner reaches the caller's free-name lookup."""
    owner = _owner(caller_name)
    caller = vars(owner)[caller_name]
    assert free_name in vars(owner)
    assert caller.__globals__ is vars(owner)


def test_claimed_row_logs_on_cw_dispatch(caplog: pytest.LogCaptureFixture) -> None:
    """``claimed_row`` keeps its ``cw.dispatch`` logger; no logger moves."""
    log = vars(claimed_row)["_log"]
    assert log.name == "cw.dispatch"
    with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
        log.debug("claimed_row logger pin probe")
    assert [record.name for record in caplog.records] == ["cw.dispatch"]


def test_leaves_bind_no_logger() -> None:
    """No moved body logs, so no leaf binds ``_log`` or ``_LOGGER_NAME``."""
    for leaf in _LEAVES:
        namespace = vars(importlib.import_module(leaf))
        assert "_log" not in namespace
        assert "_LOGGER_NAME" not in namespace


def test_leaves_import_cold_without_the_cycle() -> None:
    """Each leaf imports cold without loading any dispatch/reconcile module."""
    for leaf in _LEAVES:
        probe = (
            "import sys\n"
            f"import {leaf}\n"
            f"exact = {_CYCLE_EXACT!r}\n"
            f"prefixes = {_CYCLE_PREFIXES!r}\n"
            "bad = sorted(k for k in sys.modules"
            " if k in exact or k.startswith(prefixes))\n"
            "assert not bad, bad\n"
        )
        subprocess.run(
            [sys.executable, "-I", "-c", probe],
            check=True,
            env=os.environ.copy(),
        )
