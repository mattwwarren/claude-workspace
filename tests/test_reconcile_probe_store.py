"""Raw contract and pure-move guard for ``cw.reconcile.probe_store`` (#2548).

#2548 moves the bounded probe store out of ``cw.reconcile.gate_plan_probes``
(#2545) into a public leaf, so the dirty-check pre-pass can share it. The
behavioral mechanics (strict ``<`` age boundary, negative age, ``captured_at``
stamped before the read, spent budget, cap, failed reads counting) stay pinned
through ``PlanProbes`` in ``tests/test_reconcile_gate_plan_probes.py``; this
file pins only the raw store's reason/age codes and the move itself:

1. Each moved name is defined in ``probe_store`` and the old module still
   binds the two names it uses, to the same objects. ``_StoredProbe`` is not
   re-bound there, so a half-move is caught.
2. The old module keeps every other name it defined before the move.
3. ``monotonic`` is looked up from ``probe_store``, which is why the budget
   tests patch it there.
4. ``probe_store`` binds no logger and imports only the standard library.
"""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest
from freezegun import freeze_time

from cw.reconcile import gate_plan_probes, probe_store
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
_MAX_AGE = 10.0
_PROBE_STORE = "cw.reconcile.probe_store"

# Owning module for every name #2548 moves.
_OWNER: dict[str, str] = {
    "BoundedProbeStore": _PROBE_STORE,
    "ProbeStoreUnavailableError": _PROBE_STORE,
    "StoredProbe": _PROBE_STORE,
}

# Old private name in ``gate_plan_probes`` -> new public name in ``probe_store``,
# for the two names the old module still uses.
_OLD_ALIASES: dict[str, str] = {
    "_BoundedProbeStore": "BoundedProbeStore",
    "_ProbeStoreUnavailableError": "ProbeStoreUnavailableError",
}

# The 14 repo-defined module-level names ``gate_plan_probes`` had before the
# move, captured from the unmodified tree (an ``ast`` scan of its top level).
# Hardcoded, not re-derived: the move must not drop anything else.
_PRE_MOVE_NAMES = frozenset(
    {
        "PLAN_PREFETCH_BUDGET_SECONDS",
        "PLAN_PREFETCH_MAX_PER_TICK",
        "PLAN_PROBE_MAX_AGE_SECONDS",
        "PlanBodyReader",
        "PlanBodySource",
        "PlanProbe",
        "PlanProbeUnavailableError",
        "PlanProbes",
        "_BoundedProbeStore",
        "_ProbeKey",
        "_ProbeStoreUnavailableError",
        "_StoredProbe",
        "_probe_key",
        "lookup_plan_probe",
    }
)
_MOVED_OLD_NAMES = frozenset(
    {"_BoundedProbeStore", "_ProbeStoreUnavailableError", "_StoredProbe"}
)


def _store(
    *, budget: float | None = None, cap: int | None = None
) -> BoundedProbeStore[str, str]:
    return BoundedProbeStore(
        budget_seconds=budget, max_captures=cap, max_age_seconds=_MAX_AGE
    )


def _defined_names(module: ModuleType) -> set[str]:
    """Top-level names *module*'s own source defines (classes, defs, assigns)."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.TypeAlias):
            names.add(node.name.id)
    return names


class TestRawStoreContract:
    def test_capture_returns_and_stores_the_payload(self) -> None:
        store = _store()
        stamps: list[datetime] = []

        def _read(captured_at: datetime) -> str:
            stamps.append(captured_at)
            return "payload"

        with freeze_time(_NOW):
            assert store.capture("k", read=_read) == "payload"
            assert store.lookup("k") == "payload"
        assert stamps == [_NOW]
        assert store.keys == frozenset({"k"})
        assert store.captures == 1

    def test_recapture_overwrites_the_key(self) -> None:
        store = _store()
        with freeze_time(_NOW):
            store.capture("k", read=lambda _at: "old")
            store.capture("k", read=lambda _at: "new")
            assert store.lookup("k") == "new"
        assert store.keys == frozenset({"k"})
        assert store.captures == 2

    def test_missing_key_reason(self) -> None:
        with pytest.raises(ProbeStoreUnavailableError) as info:
            _store().lookup("absent")
        assert info.value.reason == "missing"
        assert info.value.age is None

    def test_stale_reason_carries_the_age(self) -> None:
        store = _store()
        with freeze_time(_NOW) as clock:
            store.capture("k", read=lambda _at: "v")
            clock.tick(_MAX_AGE)
            with pytest.raises(ProbeStoreUnavailableError) as info:
                store.lookup("k")
        assert info.value.reason == "stale"
        assert info.value.age == _MAX_AGE

    def test_negative_age_is_stale(self) -> None:
        store = _store()
        with freeze_time(_NOW) as clock:
            store.capture("k", read=lambda _at: "v")
            clock.move_to(_NOW - timedelta(seconds=1))
            with pytest.raises(ProbeStoreUnavailableError) as info:
                store.lookup("k")
        assert info.value.reason == "stale"
        assert info.value.age == -1.0

    def test_cap_reason(self) -> None:
        store = _store(cap=1)
        store.capture("a", read=lambda _at: "v")
        with pytest.raises(ProbeStoreUnavailableError) as info:
            store.capture("b", read=lambda _at: "v")
        assert info.value.reason == "cap"
        assert store.max_captures == 1

    def test_budget_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(probe_store, "monotonic", lambda: clock["now"])
        store = _store(budget=5.0)
        clock["now"] = 5.0
        with pytest.raises(ProbeStoreUnavailableError) as info:
            store.capture("a", read=lambda _at: "v")
        assert info.value.reason == "budget"
        assert store.budget_seconds == 5.0
        assert store.captures == 0

    def test_none_budget_and_cap_are_unbounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(probe_store, "monotonic", lambda: clock["now"])
        store = _store()
        for index in range(50):
            clock["now"] += 1000.0
            store.capture(str(index), read=lambda _at: "v")
        assert store.captures == 50


class TestPureMove:
    def test_each_moved_name_is_defined_in_its_owner(self) -> None:
        for name, owner in _OWNER.items():
            module = sys.modules[owner]
            assert vars(module)[name].__module__ == owner, name

    def test_old_names_are_the_owners_objects(self) -> None:
        for old, new in _OLD_ALIASES.items():
            assert vars(gate_plan_probes)[old] is vars(probe_store)[new], old

    def test_stored_probe_is_not_rebound_in_the_old_module(self) -> None:
        assert "_StoredProbe" not in vars(gate_plan_probes)
        assert "StoredProbe" not in vars(gate_plan_probes)

    def test_old_module_keeps_every_unmoved_name(self) -> None:
        remaining = _PRE_MOVE_NAMES - _MOVED_OLD_NAMES
        assert len(remaining) == 11
        assert remaining <= _defined_names(gate_plan_probes)
        assert not _MOVED_OLD_NAMES & _defined_names(gate_plan_probes)

    def test_probe_store_defines_exactly_the_moved_names(self) -> None:
        assert _defined_names(probe_store) == set(_OWNER)

    def test_plan_constants_and_probe_stay_in_the_old_module(self) -> None:
        for name in ("PlanProbe", "PlanProbes", "PlanProbeUnavailableError"):
            assert vars(gate_plan_probes)[name].__module__ == gate_plan_probes.__name__
        assert "PLAN_PROBE_MAX_AGE_SECONDS" in vars(gate_plan_probes)
        assert "PLAN_PROBE_MAX_AGE_SECONDS" not in vars(probe_store)


class TestSeam:
    def test_monotonic_is_looked_up_from_probe_store(self) -> None:
        assert BoundedProbeStore.capture.__globals__ is vars(probe_store)
        assert BoundedProbeStore.__init__.__globals__ is vars(probe_store)
        assert "monotonic" in vars(probe_store)
        assert "monotonic" not in vars(gate_plan_probes)

    def test_probe_store_binds_no_logger(self) -> None:
        assert "_log" not in vars(probe_store)
        assert "logging" not in vars(probe_store)

    def test_probe_store_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(inspect.getsource(probe_store))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                roots.add(node.module.split(".")[0])
        assert roots
        assert roots <= sys.stdlib_module_names | {"__future__"}, roots
