"""Import boundary for ``cw.stage_verification`` (RFC 0019 H3, #3710).

The stage-verification package is harness-neutral: no module in it may import a
codex runner, driver, lane config, usage, argv/prompt builder or profile module.
That is enforced here as a DIRECT-import scan at any nesting depth (module
scope, function scope and ``TYPE_CHECKING`` bodies), with a shrink-only
allowlist for the codex coupling that remains.

Caveat: the rule is direct, not transitive. ``cw.codex_review.__init__`` pulls
``_roles`` and ``core`` (which reach ``codex_runner`` and ``_profile``), and
``cw.codex_review_persist`` imports ``codex_runner``. A transitive purity test
is unreachable without moving ``codex_review`` constants. Remaining coupling is
recorded in ``_ALLOWED`` and tracked by follow-up #3718 for
the ``codex_fix_loop`` modules (``gates``, ``gate_autofix``, ``hook_failure``,
``guard_script``).
"""

from __future__ import annotations

import ast
import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from tests._package_split_helpers import (
    SRC_ROOT,
    _module_name,
    _package_files,
    _run_cold,
)

_PACKAGE = "cw.stage_verification"

_EXPECTED_MODULES = frozenset(
    {
        "__init__",
        "baseline",
        "fence",
        "finalize_arm",
        "finalize_ci_evidence",
        "finalize_gates",
        "finalize_merge_gate",
        "finalize_pr",
        "finalize_result",
        "growth",
        "impl_block",
        "impl_feedback",
        "impl_gates",
        "plan_gates",
        "posted_text",
        "push",
    }
)

#: Old dotted paths that must no longer resolve (the 14 ``git mv`` sources).
_OLD_PATHS = (
    "cw.codex_finalize._gates",
    "cw.codex_finalize._ci_evidence",
    "cw.codex_finalize._merge_gate",
    "cw.codex_finalize._pr",
    "cw.codex_finalize._arm",
    "cw.codex_finalize._result",
    "cw.codex_impl._gates",
    "cw.codex_impl._feedback",
    "cw.codex_plan._gates",
    "cw.codex_fix_loop.fence",
    "cw.codex_fix_loop.growth",
    "cw.codex_fix_loop.push",
    "cw.codex_fix_loop.baseline",
    "cw.codex_fix_loop.posted_text",
)

#: Codex packages whose surviving submodule sets are pinned.
_REMAINING_CODEX_MODULES = {
    "cw.codex_finalize": {"__init__", "_argv", "_post", "_prompt", "_run"},
    "cw.codex_impl": {"__init__", "_argv", "_prompt", "_result", "_run"},
    "cw.codex_plan": {
        "__init__",
        "_argv",
        "_post",
        "_prompt",
        "_result",
        "_run",
        "_stations",
    },
}

_FORBIDDEN_EXACT = frozenset(
    {
        "cw.codex_runner",
        "cw.codex_driver",
        "cw.codex_lane_config",
        "cw.codex_usage",
        "cw.executor.codex",
        "cw.codex_review._profile",
        "cw.codex_review._roles",
        "cw.codex_review._shard_run",
    }
)

#: Remaining ``cw.codex_*`` coupling, keyed ``(module, imported target)``. Shrink
#: only: a stale entry fails the ratchet. Module-level keys (a symbol imported
#: from ``cw.codex_review`` is keyed to ``cw.codex_review``, not the symbol).
_ALLOWED: Counter[tuple[str, str]] = Counter(
    {
        ("baseline", "cw.codex_review_persist"): 2,
        ("fence", "cw.codex_review"): 8,
        ("finalize_ci_evidence", "cw.codex_review"): 1,
        ("finalize_gates", "cw.codex_fix_loop.gates"): 5,
        ("finalize_gates", "cw.codex_fix_loop.guard_script"): 2,
        ("finalize_gates", "cw.codex_review"): 1,
        ("finalize_merge_gate", "cw.codex_fix_loop.guard_script"): 2,
        ("finalize_result", "cw.codex_review"): 3,
        ("growth", "cw.codex_review"): 4,
        ("growth", "cw.codex_review._const"): 1,
        ("growth", "cw.codex_fix_loop.convergence"): 1,
        ("impl_gates", "cw.codex_fix_loop.gates"): 3,
        ("impl_gates", "cw.codex_fix_loop.guard_script"): 2,
        ("impl_gates", "cw.codex_fix_loop.hook_failure"): 2,
        ("impl_gates", "cw.codex_review"): 6,
    }
)


def _is_module(dotted: str) -> bool:
    """True when ``dotted`` (``cw.x.y``) names a module, not a symbol."""
    parts = dotted.split(".")[1:]
    base = SRC_ROOT.joinpath(*parts)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _resolve_targets(node: ast.AST, module: str) -> list[str]:
    """The imported targets of one ``Import``/``ImportFrom`` node."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        package = module.split(".")[:-1]
        keep = len(package) - (node.level - 1)
        root = ".".join([*package[:keep], *([node.module] if node.module else [])])
    else:
        root = node.module or ""
    targets: list[str] = []
    for alias in node.names:
        candidate = f"{root}.{alias.name}"
        if root.startswith("cw") and _is_module(candidate):
            targets.append(candidate)
        else:
            targets.append(root)
    return targets


def _scan_source(source: str, module: str) -> list[str]:
    """Every import target in *source* at any depth, resolved."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        found.extend(_resolve_targets(node, module))
    return found


def _is_forbidden(target: str) -> bool:
    last = target.rsplit(".", 1)[-1]
    return target in _FORBIDDEN_EXACT or last.endswith(("_argv", "_prompt"))


def _module_of(path: Path) -> str:
    return _module_name(path, _PACKAGE)


def _short_name(path: Path, package: str) -> str:
    """Last segment of a module name; a package's own ``__init__`` is ``__init__``."""
    dotted = _module_name(path, package)
    return "__init__" if dotted == package else dotted.rsplit(".", 1)[-1]


def _package_modules() -> list[Path]:
    return _package_files(_PACKAGE)


def test_module_set_is_pinned() -> None:
    names = {_short_name(p, _PACKAGE) for p in _package_modules()}
    assert names == _EXPECTED_MODULES
    assert len(names) == 16


@pytest.mark.parametrize("path", _package_modules(), ids=lambda p: p.stem)
def test_no_forbidden_codex_import(path: Path) -> None:
    module = _module_of(path)
    targets = _scan_source(path.read_text(encoding="utf-8"), module)
    offending = [t for t in targets if _is_forbidden(t)]
    assert offending == [], f"{module} imports forbidden modules: {offending}"


def test_codex_coupling_matches_allowlist() -> None:
    """Shrink-only ratchet: the scan equals the pinned allowlist exactly."""
    observed: Counter[tuple[str, str]] = Counter()
    for path in _package_modules():
        module = _module_of(path)
        for target in _scan_source(path.read_text(encoding="utf-8"), module):
            if target.startswith("cw.codex_"):
                observed[(path.stem, target)] += 1
    assert observed == _ALLOWED


def test_old_paths_are_gone() -> None:
    for old in _OLD_PATHS:
        assert importlib.util.find_spec(old) is None, old


@pytest.mark.parametrize("package", sorted(_REMAINING_CODEX_MODULES))
def test_remaining_codex_package_modules_are_pinned(package: str) -> None:
    files = _package_files(package)
    names = {_short_name(p, package) for p in files}
    assert names == _REMAINING_CODEX_MODULES[package]


def test_checker_flags_forbidden_shapes() -> None:
    source = (
        "from cw.codex_runner import X\n"
        "import cw.codex_finalize._argv\n"
        "def f() -> None:\n"
        "    from cw.codex_review import _roles\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from cw.codex_review._profile import P\n"
    )
    targets = _scan_source(source, f"{_PACKAGE}.synthetic")
    flagged = [t for t in targets if _is_forbidden(t)]
    assert "cw.codex_runner" in flagged
    assert "cw.codex_finalize._argv" in flagged
    assert "cw.codex_review._roles" in flagged
    assert "cw.codex_review._profile" in flagged


def test_checker_passes_clean_source_and_resolves_symbols() -> None:
    source = "from cw.codex_review import _SensitiveHit\nimport os\n"
    targets = _scan_source(source, f"{_PACKAGE}.synthetic")
    assert "cw.codex_review" in targets
    assert "cw.codex_review._SensitiveHit" not in targets
    assert not any(_is_forbidden(t) for t in targets)


@pytest.mark.parametrize(
    "module",
    [
        *sorted(f"{_PACKAGE}.{n}" for n in _EXPECTED_MODULES if n != "__init__"),
        *sorted(_REMAINING_CODEX_MODULES),
    ],
)
def test_cold_import_succeeds(module: str) -> None:
    """A new module and a codex package must each import cold, in either order."""
    _run_cold(f"import {module}")


def test_cold_import_codex_package_then_new_module() -> None:
    _run_cold("import cw.codex_finalize; import cw.stage_verification.finalize_gates")
    _run_cold("import cw.stage_verification.finalize_gates; import cw.codex_finalize")


def test_modules_are_under_size_ceiling() -> None:
    for path in _package_modules():
        lines = path.read_text(encoding="utf-8").count("\n")
        assert lines < 1000, f"{path.name}: {lines} lines"
