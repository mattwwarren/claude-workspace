"""Guard the agent-delegated ticket-work boundary (#1240).

cw keeps exactly one minimal, GitHub-only programmatic tracker client
(gh.py) for autonomous daemon actions that run with no agent/LLM session
present. Everything else — provider-portable ticket reads/writes — is
agent work, done through the agent's own native tools (gh CLI, Linear
MCP). This is a regression test for #1240: a source-level scan asserting
(a) no module outside a closed, named allowlist (gh.py alone) directly
constructs a `gh` CLI subprocess call or a raw provider-HTTP call, (b) gh.py is
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

# Modules allowed to directly construct a `gh`-led argv list/tuple: a `"gh"`
# literal, a name bound from `which("gh")` (or the conventional name `gh`), or
# an inline `which("gh")` call, as a call argument or built elsewhere. A bare
# `which("gh") is not None` presence probe builds no argv and is exempt.
# Paths are relative to _SRC_ROOT, posix form — i.e. what
# `path.relative_to(_SRC_ROOT).as_posix()` produces.
#
# gh.py is the only direct caller: every other cw module reaches GitHub
# through gh.py's typed functions, not raw subprocess calls. No grandfathered
# callers remain after #1284 consolidated cw/doctor/loop_health.py and
# cw/worktree_gc.py into gh.py (cw/reconcile/salvage.py left earlier, with the
# process-kill-timeout removal). A new entry needs a new ADR.
_DIRECT_GH_CALLER_ALLOWLIST = frozenset({"cw/gh.py"})

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


def _is_which_gh_call(node: ast.expr) -> bool:
    """True if *node* is a ``shutil.which("gh")`` / ``which("gh")`` call."""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    named_which = (isinstance(func, ast.Attribute) and func.attr == "which") or (
        isinstance(func, ast.Name) and func.id == "which"
    )
    first = node.args[0]
    return named_which and isinstance(first, ast.Constant) and first.value == "gh"


def _yields_which_gh(value: ast.expr) -> bool:
    """True if *value* is a ``which("gh")`` call, or an ``or`` chain holding one."""
    if isinstance(value, ast.BoolOp):
        return any(_yields_which_gh(operand) for operand in value.values)
    return _is_which_gh_call(value)


def _resolved_gh_names(tree: ast.AST) -> set[str]:
    """Names that hold a resolved ``gh`` path: ``gh`` itself plus any binding.

    ``gh`` is always included, so a helper parameter such as
    ``_open_pr_argv(gh: str, ...)`` is caught without dataflow. Any other name
    bound from ``which("gh")`` (assignment, annotated assignment or walrus) is
    added.
    """
    names = {"gh"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _yields_which_gh(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, (ast.AnnAssign, ast.NamedExpr))
            and node.value is not None
            and _yields_which_gh(node.value)
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    return names


def _is_gh_argv_literal(node: ast.AST, gh_names: set[str]) -> bool:
    """True if *node* is a Load-context list/tuple whose first element is gh.

    "gh" means the literal ``"gh"``, a name in *gh_names*, or an inline
    ``which("gh")`` call. Store-context targets (``gh, rest = pair``,
    ``for gh, x in pairs``) are unpacking, not argv construction, and are
    skipped by the ``ctx`` check.
    """
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    if not isinstance(node.ctx, ast.Load):
        return False
    first = node.elts[0]
    if isinstance(first, ast.Constant):
        return first.value == "gh"
    if isinstance(first, ast.Name):
        return first.id in gh_names
    return _is_which_gh_call(first)


def _direct_gh_call_linenos(tree: ast.AST) -> list[int]:
    """Line numbers of every `gh`-led argv list/tuple, wherever it is built.

    Walks all Load-context list/tuple nodes, not only call arguments, so
    ``return [gh, ...]`` and ``cmd = ["gh", ...]`` are caught too (#2798). A
    bare ``which("gh")`` presence probe builds no argv and is not flagged.
    """
    gh_names = _resolved_gh_names(tree)
    return [
        node.lineno for node in ast.walk(tree) if _is_gh_argv_literal(node, gh_names)
    ]


def _find_direct_gh_subprocess_call(tree: ast.AST, path: Path) -> list[str]:
    """Flag direct `gh` argv construction outside the sanctioned allowlist."""
    rel = path.relative_to(_SRC_ROOT).as_posix()
    if rel in _DIRECT_GH_CALLER_ALLOWLIST:
        return []
    return [
        f"{path}:{lineno}: direct 'gh' subprocess call construction (or an argv "
        "built from a resolved gh path) outside _DIRECT_GH_CALLER_ALLOWLIST "
        "— route through cw.gh instead"
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
    """Deny-list scan: no raw `gh` subprocess construction outside gh.py."""

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
            # cw/codex_review_persist.py: _post_review_comment (executor.py's
            # only cw.gh consumer) moved to cw/codex_background.py with
            # #1727's module-size split, then here with #2807's.
            "cw/codex_review_persist.py",
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


_FAKE_MODULE = _SRC_ROOT / "cw" / "fake.py"
_GH_MODULE = _SRC_ROOT / "cw" / "gh.py"

_FLAGGED_SNIPPETS = {
    "which-bound-gh-in-call": (
        "gh = shutil.which('gh')\nsubprocess.run([gh, 'issue', 'view'])\n"
    ),
    "which-bound-other-name": (
        "gh_path = shutil.which('gh')\nsubprocess.run([gh_path, 'pr'])\n"
    ),
    "inline-which-call": "subprocess.run([shutil.which('gh'), 'x'])\n",
    "bare-which-name-call": "subprocess.run([which('gh'), 'x'])\n",
    "helper-returns-list": (
        "def _argv(gh: str, t: str) -> list[str]:\n    return [gh, 'pr', 'list']\n"
    ),
    "assigned-literal-list": "cmd = ['gh', 'x']\nsubprocess.run(cmd)\n",
    "literal-tuple": "x = ('gh', 'x')\n",
    "annotated-which-bound": ("p: str | None = shutil.which('gh')\nx = [p, 'a']\n"),
    "walrus-which-bound": "if (p := shutil.which('gh')):\n    x = [p, 'a']\n",
    "which-with-or-fallback": (
        "p = shutil.which('gh') or '/usr/bin/gh'\nx = [p, 'a']\n"
    ),
}

_CLEAN_SNIPPETS = {
    "presence-probe": "def f() -> bool:\n    return shutil.which('gh') is not None\n",
    "which-other-binary": "cw = shutil.which('cw')\nx = [cw, 'doctor']\n",
    "other-literals": "a = ['git', 'x']\nb = [1, 2]\nc = []\n",
    "unpacking-assignment": "gh, rest = pair\n",
    "parenthesized-unpacking": "(gh, rest) = pair\n",
    "for-target-unpacking": "for gh, x in pairs:\n    pass\n",
    "non-which-call-binding": "p = shutil.which('git')\nx = [p, 'a']\n",
}


class TestScannerSelfChecks:
    """The gh-argv scanner flags what it claims to and spares what it should (#2798).

    Positive and negative controls over small source snippets, so a future
    edit to the scanner cannot silently narrow it back to ``["gh", ...]``
    literals passed straight to a call.
    """

    @staticmethod
    def _scan(source: str, path: Path = _FAKE_MODULE) -> list[str]:
        return _find_direct_gh_subprocess_call(ast.parse(source), path)

    def test_flagged_snippets_are_flagged(self) -> None:
        missed = [
            name for name, src in _FLAGGED_SNIPPETS.items() if not self._scan(src)
        ]
        assert not missed, f"scanner failed to flag: {missed}"

    def test_clean_snippets_are_not_flagged(self) -> None:
        wrongly = [name for name, src in _CLEAN_SNIPPETS.items() if self._scan(src)]
        assert not wrongly, f"scanner wrongly flagged: {wrongly}"

    def test_presence_probe_is_not_flagged(self) -> None:
        assert self._scan(_CLEAN_SNIPPETS["presence-probe"]) == []

    def test_unpacking_assignment_is_not_flagged(self) -> None:
        assert self._scan(_CLEAN_SNIPPETS["unpacking-assignment"]) == []
        assert self._scan(_CLEAN_SNIPPETS["parenthesized-unpacking"]) == []

    def test_for_target_unpacking_is_not_flagged(self) -> None:
        assert self._scan(_CLEAN_SNIPPETS["for-target-unpacking"]) == []

    def test_allowlisted_module_is_exempt(self) -> None:
        for name, src in _FLAGGED_SNIPPETS.items():
            assert self._scan(src, _GH_MODULE) == [], name

    def test_violation_names_the_line_and_cw_gh(self) -> None:
        violations = self._scan("x = 1\ncmd = ['gh', 'x']\n")
        assert len(violations) == 1
        assert f"{_FAKE_MODULE}:2:" in violations[0]
        assert "route through cw.gh" in violations[0]
