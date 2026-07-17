"""Guard the agent-delegated ticket-work boundary (#1240).

cw keeps exactly one minimal, GitHub-only programmatic tracker client
(gh.py) for autonomous daemon actions that run with no agent/LLM session
present. Everything else — provider-portable ticket reads/writes — is
agent work, done through the agent's own native tools (gh CLI, Linear
MCP). This is a regression test for #1240: a source-level scan asserting
(a) no module outside a closed, named allowlist directly constructs a
`gh` CLI subprocess call or a raw provider-HTTP call, (b) gh.py is
imported (not bypassed) by every other cw module that needs GitHub data,
and (c) no Linear/tracker SDK import exists anywhere in src/cw. See
docs/adr/0013-agent-delegated-ticket-work.md for the invariant this
test enforces.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

from tests.conftest import _REPO_ROOT, _SRC_ROOT, _iter_src_files

# Modules allowed to directly construct a `["gh", ...]` (or `("gh", ...)`)
# subprocess argv literal. Paths are relative to _SRC_ROOT, posix form —
# i.e. what `path.relative_to(_SRC_ROOT).as_posix()` produces.
_DIRECT_GH_CALLER_ALLOWLIST = frozenset(
    {
        # The sanctioned surface: every other cw module reaches GitHub
        # through gh.py's typed functions, not raw subprocess calls.
        "cw/gh.py",
        # Pre-existing daemon-side direct callers, grandfathered.
        # Consolidation into gh.py is tracked as a follow-up in #1284,
        # not this ticket's scope.
        "cw/doctor.py",
        "cw/worktree_gc.py",
        "cw/reconcile/salvage.py",
    }
)

# Package-name substrings that would indicate a Linear/tracker SDK import,
# or a generic HTTP client — cw has zero HTTP-client dependency today, and
# adding one for tracker I/O would be exactly the regression this test
# exists to catch. AST-based (import-node walking), not regex, specifically
# so a string/comment mentioning "Linear" (already legitimate prose in
# gh.py's docstring, models.py, doctor.py, etc.) never false-trips.
_LINEAR_SDK_DENYLIST = frozenset(
    {
        "linear_sdk",
        "linear_api",
        "pylinear",
        "requests",
        "httpx",
    }
)


def _is_gh_argv_literal(node: ast.expr) -> bool:
    """True if *node* is a list/tuple literal whose first element is "gh"."""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        first = node.elts[0]
        return isinstance(first, ast.Constant) and first.value == "gh"
    return False


def _direct_gh_call_linenos(tree: ast.AST) -> list[int]:
    """Line numbers of every direct `["gh", ...]`-shaped call construction."""
    linenos: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        candidates = [*node.args, *(kw.value for kw in node.keywords)]
        if any(_is_gh_argv_literal(candidate) for candidate in candidates):
            linenos.append(node.lineno)
    return linenos


def _find_direct_gh_subprocess_call(tree: ast.AST, path: Path) -> list[str]:
    """Flag direct `gh` argv construction outside the sanctioned allowlist."""
    rel = path.relative_to(_SRC_ROOT).as_posix()
    if rel in _DIRECT_GH_CALLER_ALLOWLIST:
        return []
    return [
        f"{path}:{lineno}: direct 'gh' subprocess call construction outside "
        f"_DIRECT_GH_CALLER_ALLOWLIST — route through cw.gh instead"
        for lineno in _direct_gh_call_linenos(tree)
    ]


def _find_sdk_imports(tree: ast.AST, path: Path) -> list[str]:
    """Flag any import whose top-level module name is on the SDK denylist."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", maxsplit=1)[0]
                if top in _LINEAR_SDK_DENYLIST:
                    violations.append(
                        f"{path}:{node.lineno}: forbidden import '{alias.name}'"
                    )
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            top = node.module.split(".", maxsplit=1)[0]
            if top in _LINEAR_SDK_DENYLIST:
                violations.append(
                    f"{path}:{node.lineno}: forbidden import from '{node.module}'"
                )
    return violations


def _run_scan(finder: Callable[[ast.AST, Path], list[str]]) -> list[str]:
    """Drive *finder* over every src/ file's parsed AST.

    Kept file-local rather than hoisted alongside `_iter_src_files` /
    `_SRC_ROOT`: `test_review_approval_guard.py`'s precedent `_run_scan`
    takes a `Callable[[str, Path], list[str]]` finder over raw file text
    (regex-based scanning); this ticket's finders are AST-based
    (`Callable[[ast.AST, Path], list[str]]`, driven off `ast.parse`). The
    two driver functions have genuinely different signatures and
    semantics, not just cosmetic duplication.
    """
    violations: list[str] = []
    for path in _iter_src_files():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        violations.extend(finder(tree, path))
    return violations


class TestNoDirectGhCallOutsideAllowlist:
    """Deny-list scan: no raw `gh` subprocess construction outside gh.py
    (and the three named, grandfathered daemon-side callers)."""

    def test_no_direct_gh_call_outside_allowlist(self) -> None:
        violations = _run_scan(_find_direct_gh_subprocess_call)
        assert not violations, "\n".join(violations)


class TestNoLinearOrTrackerSdkImports:
    """Deny-list scan: no Linear/tracker SDK or generic HTTP client import."""

    def test_no_linear_or_tracker_sdk_imports(self) -> None:
        violations = _run_scan(_find_sdk_imports)
        assert not violations, "\n".join(violations)


class TestAllowlistStaysHonest:
    """Positive controls: the allowlist can't silently go stale."""

    def test_allowlist_entries_still_exist_and_call_gh(self) -> None:
        """Each allowlisted module must still directly construct a `gh` call.

        Guards against the allowlist accumulating entries for modules that
        no longer need the exception — mirrors
        test_review_approval_guard.py's "legitimate neighbors stay green"
        positive control, inverted.
        """
        for rel in sorted(_DIRECT_GH_CALLER_ALLOWLIST):
            path = _SRC_ROOT / rel
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            assert _direct_gh_call_linenos(tree), (
                f"{rel} is in _DIRECT_GH_CALLER_ALLOWLIST but no longer "
                "contains a direct 'gh' call construction — remove it "
                "from the allowlist"
            )

    def test_gh_py_is_imported_by_its_known_callers(self) -> None:
        """Spot-check real call sites still import from cw.gh, not bypass it."""
        known_callers = (
            "cw/executor.py",
            "cw/pr_hydrate.py",
            "cw/reconcile/gate_recipes.py",
        )
        for rel in known_callers:
            path = _SRC_ROOT / rel
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports_gh = any(
                isinstance(node, ast.ImportFrom) and node.module == "cw.gh"
                for node in ast.walk(tree)
            )
            assert imports_gh, f"{rel} no longer imports from cw.gh"


class TestNoHttpClientDependency:
    """Dependency-level signal alongside the import-scan (#1240)."""

    def test_no_http_client_dependency(self) -> None:
        pyproject_path = _REPO_ROOT / "pyproject.toml"
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)

        deps: list[str] = list(data["project"]["dependencies"])
        for extra in data["project"].get("optional-dependencies", {}).values():
            deps.extend(extra)

        forbidden = {"requests", "httpx"}
        for dep in deps:
            name = re.split(r"[><=!\[; ]", dep, maxsplit=1)[0].strip()
            assert name not in forbidden, f"forbidden HTTP client dependency: {dep}"
