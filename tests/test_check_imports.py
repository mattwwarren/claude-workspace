"""Tests for .claude/scripts/check_imports.py (CI Quality Gate #5, #1850).

The gate's ``GROUPS`` table is a hand-maintained enumeration: a new script
under ``.claude/scripts/`` is only smoke-imported if someone remembers to add
it. This file pins the entry for ``classify_merge_conflict`` (#1850) and
exercises ``main()`` end-to-end so an import-time defect in *any* enumerated
script fails here as well as in CI.

Loaded via importlib, mirroring ``tests/test_check_plan_scope_conformance.py``
— ``check_imports.py`` lives outside the ``src/`` tree.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_imports.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_imports", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_imports", mod)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


def _scripts_group() -> list[str]:
    for pythonpath, modules in _mod.GROUPS:
        if pythonpath == ".claude/scripts":
            return list(modules)
    raise AssertionError("no '.claude/scripts' entry in check_imports.GROUPS")


def test_classify_merge_conflict_in_groups() -> None:
    assert "classify_merge_conflict" in _scripts_group()


def test_every_enumerated_script_exists_on_disk() -> None:
    for pythonpath, modules in _mod.GROUPS:
        for module in modules:
            assert (_REPO_ROOT / pythonpath / f"{module}.py").is_file(), (
                f"{pythonpath}/{module}.py enumerated in GROUPS but missing"
            )


def test_main_smoke_imports_succeed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    assert _mod.main() == 0
    assert "imported successfully" in capsys.readouterr().out
