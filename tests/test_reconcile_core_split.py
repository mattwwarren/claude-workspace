"""Seam assertions for the ``cw.reconcile.core`` split (#2896).

``cw/reconcile/core.py`` passed CLAUDE.md's ~1000-line module ceiling, and
``reconcile()`` / ``_reconcile_locked`` sat a few statements under ruff's
``PLR0915`` limit. #2896 moves code verbatim into two sibling modules:
``prepasses`` (the lockless pre-lock captures, including the gh merge-state
loop) and ``locked_sweeps`` (the in-lock sweep wiring). ``core`` stays a plain
module that keeps ``reconcile``, ``_reconcile_locked``, ``_drain_deferred_jobs``
and the single ``sessions_lock``.

The behavioral suite cannot see five things the move must keep true:

1. The surface survives: ``reconcile``, ``_reconcile_locked`` and
   ``_verify_supervisor_session_id`` still resolve from ``cw.reconcile``.
2. Each name is *defined in* its owning module, so a patch on the owner
   reaches the real call site.
3. The patch seams live where the tests patch them. ``_claude_agents_json`` is
   read by core's ``reconcile`` alone (it hands it to the tick's roster
   snapshot, #2550: bound into the dirty-check capture's ``roster=`` and
   refreshed last before the lock), so the many
   ``cw.reconcile.core._claude_agents_json`` patches
   keep working; a name core no longer reads is not bound there, so a stale
   ``cw.reconcile.core.<name>`` patch fails loudly instead of silently.
4. ``reconcile()`` keeps the ADR-0019 order: every pre-pass before the one
   bounded ``sessions_lock``, the drain in its ``finally``, then the post-lock
   passes; and the harvest capture stays the last git/gh pre-pass, so only
   the bounded roster refresh (#2550) follows it.
5. The split stays small: each module is under the size flag and the two
   orchestrators keep statement headroom under ``PLR0915``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw.reconcile
from cw.reconcile import core, locked_sweeps, prepasses
from tests._lock_invariants import SUBPROCESS_UNDER_SESSIONS_ALLOWLIST

if TYPE_CHECKING:
    from types import ModuleType

_CORE = "cw.reconcile.core"
_PREPASSES = "cw.reconcile.prepasses"
_LOCKED_SWEEPS = "cw.reconcile.locked_sweeps"
_SPLIT_MODULES = (_CORE, _PREPASSES, _LOCKED_SWEEPS)

# Owning module for every function the split places. Hardcoded, not derived,
# so a function that drifts to another module fails.
_OWNER: dict[str, str] = {
    "reconcile": _CORE,
    "_reconcile_locked": _CORE,
    "_drain_deferred_jobs": _CORE,
    "_capture_merge_state": _PREPASSES,
    "_capture_codex_clean_probes": _PREPASSES,
    "_capture_plan_probes": _PREPASSES,
    "_capture_dirty_checks": _PREPASSES,
    "_capture_harvest_facts": _PREPASSES,
    "_capture_closing_prepasses": _PREPASSES,
    "_run_terminal_backstops_and_sweeps": _LOCKED_SWEEPS,
    "_without_acts_in_flight": _LOCKED_SWEEPS,
    "_verify_supervisor_session_id": _LOCKED_SWEEPS,
    "_run_pre_guard_sweeps": _LOCKED_SWEEPS,
    "_run_post_guard_sweeps": _LOCKED_SWEEPS,
}

# Names ``cw.reconcile`` re-exports from the split modules.
_PACKAGE_SURFACE = ("reconcile", "_reconcile_locked", "_verify_supervisor_session_id")

_BACKSTOPS = ("_run_terminal_backstops_and_sweeps",)

# (patched name, module that must bind it, reader functions in that module
# whose globals the patch has to reach).
_SEAMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("_claude_agents_json", _CORE, ("reconcile",)),
    ("RosterSnapshot", _CORE, ("reconcile",)),
    ("lookup_roster", _CORE, ("_reconcile_locked",)),
    ("normalize_roster", _CORE, ("_reconcile_locked",)),
    ("load_orchestrator_config", _CORE, ("reconcile", "_reconcile_locked")),
    ("load_clients", _CORE, ("reconcile", "_reconcile_locked")),
    ("_detect_phantom_candidates", _CORE, ("_reconcile_locked",)),
    ("_looks_like_daemon_outage", _CORE, ("_reconcile_locked",)),
    ("complete_timed_out_merged_tasks", _CORE, ("reconcile",)),
    ("run_fix_dispatch", _CORE, ("reconcile",)),
    ("capture_review_repo_slugs", _CORE, ("reconcile",)),
    ("run_post_lock_jobs", _CORE, ("_drain_deferred_jobs",)),
    ("dispatch_deferred_review_jobs", _CORE, ("_drain_deferred_jobs",)),
    ("_capture_merge_state", _CORE, ("reconcile",)),
    ("_capture_codex_clean_probes", _CORE, ("reconcile",)),
    ("_capture_plan_probes", _CORE, ("reconcile",)),
    ("_capture_dirty_checks", _CORE, ("reconcile",)),
    ("_capture_closing_prepasses", _CORE, ("reconcile",)),
    ("_run_pre_guard_sweeps", _CORE, ("_reconcile_locked",)),
    ("_run_post_guard_sweeps", _CORE, ("_reconcile_locked",)),
    ("_run_terminal_backstops_and_sweeps", _CORE, ("_reconcile_locked",)),
    ("_without_acts_in_flight", _CORE, ("_reconcile_locked",)),
    (
        "load_state",
        _PREPASSES,
        (
            "_capture_codex_clean_probes",
            "_capture_plan_probes",
            "_capture_dirty_checks",
            "_capture_harvest_facts",
        ),
    ),
    (
        "load_dev_queue",
        _PREPASSES,
        (
            "_capture_merge_state",
            "_capture_codex_clean_probes",
            "_capture_plan_probes",
            "_capture_dirty_checks",
            "_capture_harvest_facts",
        ),
    ),
    ("capture_shipped_pr_probes", _PREPASSES, ("_capture_closing_prepasses",)),
    (
        "capture_drift_and_cancelled_probes",
        _PREPASSES,
        ("_capture_closing_prepasses",),
    ),
    ("capture_salvage_scope_probes", _PREPASSES, ("_capture_closing_prepasses",)),
    ("_capture_harvest_facts", _PREPASSES, ("_capture_closing_prepasses",)),
    ("capture_local_harvest_facts", _PREPASSES, ("_capture_harvest_facts",)),
    ("load_dev_queue", _LOCKED_SWEEPS, ("_without_acts_in_flight",)),
    ("run_unowned_running_recovery", _LOCKED_SWEEPS, _BACKSTOPS),
    ("revert_timed_out_tasks", _LOCKED_SWEEPS, _BACKSTOPS),
    (
        "revert_completed_silent_tasks",
        _LOCKED_SWEEPS,
        _BACKSTOPS,
    ),
    ("park_terminal_sibling_tasks", _LOCKED_SWEEPS, _BACKSTOPS),
    ("run_lost_session_recovery", _LOCKED_SWEEPS, _BACKSTOPS),
    ("run_concierge_recoveries", _LOCKED_SWEEPS, _BACKSTOPS),
    (
        "run_codex_live_writer_reparks",
        _LOCKED_SWEEPS,
        _BACKSTOPS,
    ),
    ("run_gate_recipes", _LOCKED_SWEEPS, _BACKSTOPS),
    ("run_review_recipes", _LOCKED_SWEEPS, _BACKSTOPS),
    ("run_escalation_sweep", _LOCKED_SWEEPS, _BACKSTOPS),
    ("_detect_stalled_candidates", _LOCKED_SWEEPS, ("_run_pre_guard_sweeps",)),
    ("_act_on_local_harvest_candidates", _LOCKED_SWEEPS, ("_run_pre_guard_sweeps",)),
    ("_detect_main_drift_candidates", _LOCKED_SWEEPS, ("_run_pre_guard_sweeps",)),
    ("sweep_leaked_daemon_workers", _LOCKED_SWEEPS, ("_run_pre_guard_sweeps",)),
    ("_detect_idle_candidates", _LOCKED_SWEEPS, ("_run_post_guard_sweeps",)),
    ("record_session_liveness_changes", _LOCKED_SWEEPS, ("_run_post_guard_sweeps",)),
    ("run_shipped_release", _LOCKED_SWEEPS, ("_run_post_guard_sweeps",)),
    ("sweep_routed_result_sessions", _LOCKED_SWEEPS, ("_run_post_guard_sweeps",)),
    (
        "detect_and_park_mid_turn_usage_limits",
        _LOCKED_SWEEPS,
        ("_run_post_guard_sweeps",),
    ),
)

# Names core read before the split but no longer binds: a stale
# ``cw.reconcile.core.<name>`` patch must raise, not silently miss.
_DROPPED_FROM_CORE = (
    "_deps",
    "_capture_harvest_facts",
    "_verify_supervisor_session_id",
    "capture_shipped_pr_probes",
    "capture_drift_and_cancelled_probes",
    "capture_salvage_scope_probes",
    "capture_local_harvest_facts",
    "run_unowned_running_recovery",
    "revert_timed_out_tasks",
    "revert_completed_silent_tasks",
    "park_terminal_sibling_tasks",
    "run_lost_session_recovery",
    "run_concierge_recoveries",
    "run_codex_live_writer_reparks",
    "run_gate_recipes",
    "run_review_recipes",
    "run_escalation_sweep",
    "_detect_stalled_candidates",
    "_act_on_local_harvest_candidates",
    "_detect_main_drift_candidates",
    "_detect_idle_candidates",
    "record_session_liveness_changes",
    "run_shipped_release",
    "sweep_routed_result_sessions",
)

#: CLAUDE.md's "Module Size" yellow flag; core.py was 1056 lines.
_MODULE_LINE_LIMIT = 800

#: Statement ceilings, well under PLR0915's 50. This file's counter measured
#: ``reconcile`` at 23 and ``_reconcile_locked`` at 33 before #2550; the
#: tick-scoped roster snapshot (#2550) adds two statements to ``reconcile``
#: (its construction and the final refresh), which reaches the ceiling with
#: no headroom, and moves ``_reconcile_locked``'s roster read to a lookup.
_RECONCILE_MAX_STATEMENTS = 25
_RECONCILE_LOCKED_MAX_STATEMENTS = 36

# reconcile()'s pre-lock captures, in the order ADR-0019 pins.
_PRE_PASS_ORDER = (
    "_capture_merge_state",
    "_capture_codex_clean_probes",
    "_capture_plan_probes",
    "capture_review_repo_slugs",
    "_capture_dirty_checks",
    "_capture_closing_prepasses",
    # #2550: the roster refresh is the final pre-lock step, after harvest.
    "refresh",
)

_CLOSING_ORDER = (
    "capture_shipped_pr_probes",
    "capture_drift_and_cancelled_probes",
    "capture_salvage_scope_probes",
    "_capture_harvest_facts",
)


def _module(name: str) -> ModuleType:
    return importlib.import_module(name)


def _function_def(module: ModuleType, name: str) -> ast.FunctionDef:
    path = Path(inspect.getfile(module))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    msg = f"{name} not defined at the top level of {module.__name__}"
    raise AssertionError(msg)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _calls(node: ast.AST) -> list[ast.Call]:
    """Every call under *node*, in source order."""
    found = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
    return sorted(found, key=lambda c: (c.lineno, c.col_offset))


def _first_call_line(fn: ast.FunctionDef, name: str) -> int:
    lines = [c.lineno for c in _calls(fn) if _call_name(c) == name]
    assert lines, f"{fn.name} never calls {name}"
    return lines[0]


def _count_statements(body: list[ast.stmt]) -> int:
    """Statements under *body*, recursing into compound bodies, not nested defs."""
    count = 0
    for stmt in body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            count += 1
            continue
        count += 1
        for field in ("body", "orelse", "finalbody"):
            count += _count_statements(getattr(stmt, field, []))
        for handler in getattr(stmt, "handlers", []):
            count += 1 + _count_statements(handler.body)
    return count


def _function_statements(fn: ast.FunctionDef) -> int:
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return _count_statements(body)


# ---------------------------------------------------------------------------
# Surface and ownership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _PACKAGE_SURFACE)
def test_package_surface_still_resolves(name: str) -> None:
    """``from cw.reconcile import X`` still works and ``X`` is in ``__all__``."""
    assert name in cw.reconcile.__all__
    assert getattr(cw.reconcile, name) is vars(_module(_OWNER[name]))[name]


@pytest.mark.parametrize("name", sorted(_OWNER))
def test_function_is_defined_in_its_owner(name: str) -> None:
    """Each function reports its owning module and is that module's object."""
    owner = _module(_OWNER[name])
    fn = vars(owner)[name]
    assert inspect.isfunction(fn)
    assert fn.__module__ == _OWNER[name]


@pytest.mark.parametrize(
    ("seam", "consumer", "readers"),
    _SEAMS,
    ids=[f"{seam}@{consumer.rsplit('.', 1)[-1]}" for seam, consumer, _ in _SEAMS],
)
def test_patch_seam_is_bound_where_it_is_read(
    seam: str, consumer: str, readers: tuple[str, ...]
) -> None:
    """The consumer binds the seam and its readers look it up there."""
    module = _module(consumer)
    assert seam in vars(module)
    for reader in readers:
        fn = vars(module)[reader]
        assert inspect.isfunction(fn)
        assert fn.__globals__ is vars(module)
        assert seam in fn.__code__.co_names, f"{reader} does not read {seam}"


@pytest.mark.parametrize("module", [prepasses, locked_sweeps], ids=lambda m: m.__name__)
def test_roster_reader_is_bound_only_in_core(module: ModuleType) -> None:
    """One ``_claude_agents_json`` seam: a second binding would split the patch."""
    assert "_claude_agents_json" not in vars(module)


@pytest.mark.parametrize("name", _DROPPED_FROM_CORE)
def test_moved_reader_is_not_bound_in_core(name: str) -> None:
    """A stale ``cw.reconcile.core.<name>`` patch raises instead of no-opping."""
    assert name not in vars(core)


def test_dirty_check_capture_takes_the_core_roster_reader() -> None:
    """``reconcile()`` passes ``roster=<snapshot>.shared(_claude_agents_json)``:
    core's reader, bound to the tick's roster snapshot (#2550)."""
    fn = _function_def(core, "reconcile")
    (call,) = [c for c in _calls(fn) if _call_name(c) == "_capture_dirty_checks"]
    roster = {kw.arg: kw.value for kw in call.keywords}["roster"]
    assert isinstance(roster, ast.Call)
    assert isinstance(roster.func, ast.Attribute)
    assert roster.func.attr == "shared"
    (arg,) = roster.args
    assert isinstance(arg, ast.Name)
    assert arg.id == "_claude_agents_json"


# ---------------------------------------------------------------------------
# ADR-0019 order
# ---------------------------------------------------------------------------


def test_reconcile_takes_exactly_one_bounded_sessions_lock() -> None:
    fn = _function_def(core, "reconcile")
    (lock,) = [c for c in _calls(fn) if _call_name(c) == "sessions_lock"]
    keywords = {kw.arg: kw.value for kw in lock.keywords}
    bounded = keywords["bounded"]
    assert isinstance(bounded, ast.Constant)
    assert bounded.value is True
    assert "timeout_s" not in keywords


def test_reconcile_runs_every_pre_pass_in_order_before_the_lock() -> None:
    fn = _function_def(core, "reconcile")
    lines = [_first_call_line(fn, name) for name in _PRE_PASS_ORDER]
    assert lines == sorted(lines)
    lock_line = _first_call_line(fn, "sessions_lock")
    assert lines[-1] < lock_line < _first_call_line(fn, "_reconcile_locked")


def test_reconcile_loads_state_clients_and_config_before_the_merge_capture() -> None:
    fn = _function_def(core, "reconcile")
    order = ("load_state", "load_clients", "load_orchestrator_config")
    lines = [_first_call_line(fn, name) for name in order]
    assert lines == sorted(lines)
    assert lines[-1] < _first_call_line(fn, "_capture_merge_state")


def test_reconcile_locked_places_the_extracted_sweep_groups_around_the_guard() -> None:
    """Roster lookup, pre-guard group, outage guard, post-guard group, phantoms.

    The post-guard group is roster-dependent, so running it before the outage
    guard would act on an empty roster after a daemon hiccup. The lookup is
    pure and runs first (#2550), so in-lock sweep time never ages the
    snapshot; only the early-return decision follows the pre-guard group.
    """
    fn = _function_def(core, "_reconcile_locked")
    order = (
        "lookup_roster",
        "_run_pre_guard_sweeps",
        "_looks_like_daemon_outage",
        "_run_post_guard_sweeps",
        "_without_acts_in_flight",
        "_detect_phantom_candidates",
    )
    lines = [_first_call_line(fn, name) for name in order]
    assert lines == sorted(lines)
    assert len(set(lines)) == len(lines)


def test_reconcile_drains_deferred_jobs_in_the_finally_of_the_lock_try() -> None:
    fn = _function_def(core, "reconcile")
    (outer,) = [
        node
        for node in fn.body
        if isinstance(node, ast.Try) and any(isinstance(s, ast.With) for s in node.body)
    ]
    drains = [
        c
        for c in _calls(ast.Module(body=outer.finalbody, type_ignores=[]))
        if _call_name(c) == "_drain_deferred_jobs"
    ]
    assert len(drains) == 1


def test_reconcile_post_lock_passes_precede_the_early_return() -> None:
    fn = _function_def(core, "reconcile")
    completed = _first_call_line(fn, "complete_timed_out_merged_tasks")
    fix_dispatch = _first_call_line(fn, "run_fix_dispatch")
    (early_return,) = [
        node
        for node in fn.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "completed_ticket_ids"
    ]
    assert _first_call_line(fn, "_drain_deferred_jobs") < completed
    assert completed < fix_dispatch < early_return.lineno


def test_harvest_capture_stays_last_among_the_closing_pre_passes() -> None:
    fn = _function_def(prepasses, "_capture_closing_prepasses")
    lines = [_first_call_line(fn, name) for name in _CLOSING_ORDER]
    assert lines == sorted(lines)
    final = fn.body[-1]
    assert isinstance(final, ast.Return)
    assert isinstance(final.value, ast.Tuple)
    last = final.value.elts[-1]
    assert isinstance(last, ast.Call)
    assert _call_name(last) == "_capture_harvest_facts"


# ---------------------------------------------------------------------------
# Final-state guards: size, statement headroom, lock allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _SPLIT_MODULES)
def test_module_is_under_the_size_flag(name: str) -> None:
    path = Path(inspect.getfile(_module(name)))
    line_count = len(path.read_text(encoding="utf-8").splitlines())
    assert line_count < _MODULE_LINE_LIMIT, path.name


@pytest.mark.parametrize(
    ("name", "ceiling"),
    [
        ("reconcile", _RECONCILE_MAX_STATEMENTS),
        ("_reconcile_locked", _RECONCILE_LOCKED_MAX_STATEMENTS),
    ],
)
def test_orchestrator_keeps_statement_headroom(name: str, ceiling: int) -> None:
    assert _function_statements(_function_def(core, name)) <= ceiling


@pytest.mark.parametrize("name", _SPLIT_MODULES)
def test_split_module_is_not_lock_allowlisted(name: str) -> None:
    """Nothing in the split may launch a subprocess under ``sessions_lock``."""
    assert name not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST
