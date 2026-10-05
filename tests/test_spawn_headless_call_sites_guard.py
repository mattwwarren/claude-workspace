"""Pin every ``spawn_create_impl`` call site's ``headless=`` value (#2031).

``headless=True`` writes ``"headless": true`` into the spawned session's
``cw-context.json``. Since ADR-0014 the Stop hook then waits for an
``AUTO_DEV_RESULT`` sentinel and defers *forever* without one: the session never
reaches a terminal state, so it pins its host slot and its worktree
(``cw worktree gc`` reports ``SKIP_LIVE``). ``headless=True`` is therefore only
correct for a prompt that emits the sentinel contract (``/auto-dev-<stage> ...
--headless``).

Every call site under ``src/cw/`` is classified here, keyed by
``(module_relpath, qualified_function_name)`` (CPython ``__qualname__``
convention) with the shape of its ``headless`` keyword:

- ``const-True`` -- literal ``True``; its ``prompt`` must be a sentinel-contract
  f-string (checked by :func:`test_headless_true_sites_use_sentinel_prompts`).
- ``const-False`` -- literal ``False``; the prompt emits no sentinel.
- ``absent`` -- keyword omitted, so it takes the ``False`` default.
- ``forwarded-name`` -- passes a caller-supplied value straight through (the
  ``cw spawn --headless`` flag).

A new or removed call site changes the scanned set and fails
:func:`test_spawn_create_impl_call_sites_match_classified_allowlist` until a
human classifies it in ``_SPAWN_CREATE_IMPL_ALLOWLIST``.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Literal

from tests.conftest import _SRC_ROOT, _iter_src_files

if TYPE_CHECKING:
    from pathlib import Path

_CALLEES = frozenset({"spawn_create_impl", "_spawn_create_impl"})
_SENTINEL_PROMPT_PREFIX = "/auto-dev"
_SENTINEL_PROMPT_FLAG = "--headless"

_HeadlessKind = Literal["const-True", "const-False", "absent", "forwarded-name"]
_Site = tuple[str, str]

_EXECUTOR = "cw/executor/core.py"
_ADDRESS_REVIEW = "cw/reconcile/review_recipes/address_review.py"
_FIX_AGENT = "cw/reconcile/review_recipes/fix_agent.py"
_ORCHESTRATE_CLI = "cw/cli/orchestrate.py"
_SPAWN_CLI = "cw/cli/spawn.py"

# (module_relpath, qualified_function_name) -> shape of the ``headless`` keyword.
_SPAWN_CREATE_IMPL_ALLOWLIST: dict[_Site, _HeadlessKind] = {
    # The one sentinel-contract caller: `/auto-dev-<stage> <id> --headless`.
    (_EXECUTOR, "ClaudeNativeExecutor.spawn"): "const-True",
    # `/address-review N` emits no sentinel and owns no dev-queue row (#2031).
    (_ADDRESS_REVIEW, "_dispatch_address_review"): "const-False",
    # The fix agent never emits a sentinel either (precedent for #2031).
    (_FIX_AGENT, "dispatch_fix_agent"): "const-False",
    ("cw/plan.py", "run_planner"): "absent",
    (_ORCHESTRATE_CLI, "orchestrate_start"): "absent",
    (_ORCHESTRATE_CLI, "orchestrator_start"): "absent",
    # `cw spawn --headless` passthrough: the click command -> its impl wrapper
    # -> cw.spawn.spawn_create_impl.
    (_SPAWN_CLI, "_spawn_create_impl"): "forwarded-name",
    (_SPAWN_CLI, "spawn"): "forwarded-name",
}


class _SpawnCallCollector(ast.NodeVisitor):
    """Collect ``(qualname, call)`` for every call to a ``spawn_create_impl``."""

    def __init__(self) -> None:
        # (kind, name) per enclosing scope; kind is "function" or "class".
        self._stack: list[tuple[str, str]] = []
        self.calls: list[tuple[str, ast.Call]] = []

    def _qualname(self) -> str:
        parts: list[str] = []
        for index, (kind, name) in enumerate(self._stack):
            parts.append(name)
            if kind == "function" and index < len(self._stack) - 1:
                parts.append("<locals>")
        return ".".join(parts)

    def _visit_scope(self, kind: str, node: ast.AST, name: str) -> None:
        self._stack.append((kind, name))
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope("function", node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope("function", node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope("class", node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name in _CALLEES:
            self.calls.append((self._qualname(), node))
        self.generic_visit(node)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _headless_kind(call: ast.Call) -> _HeadlessKind:
    value = _keyword(call, "headless")
    if value is None:
        return "absent"
    if isinstance(value, ast.Constant) and value.value is True:
        return "const-True"
    if isinstance(value, ast.Constant) and value.value is False:
        return "const-False"
    if isinstance(value, ast.Name):
        return "forwarded-name"
    msg = f"unclassifiable headless= expression: {ast.dump(value)}"
    raise AssertionError(msg)


def _scan_call_sites() -> dict[_Site, tuple[_HeadlessKind, ast.Call]]:
    found: dict[_Site, tuple[_HeadlessKind, ast.Call]] = {}
    for path in _iter_src_files():
        collector = _SpawnCallCollector()
        collector.visit(_parse(path))
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        for qualname, call in collector.calls:
            site = (relpath, qualname)
            assert site not in found, f"{site}: more than one spawn_create_impl call"
            found[site] = (_headless_kind(call), call)
    return found


def test_spawn_create_impl_call_sites_match_classified_allowlist() -> None:
    scanned = _scan_call_sites()

    unclassified = sorted(set(scanned) - set(_SPAWN_CREATE_IMPL_ALLOWLIST))
    stale = sorted(set(_SPAWN_CREATE_IMPL_ALLOWLIST) - set(scanned))
    assert not unclassified, (
        "New spawn_create_impl call site(s) -- classify each in "
        "_SPAWN_CREATE_IMPL_ALLOWLIST (headless=True is only correct for a "
        f"sentinel-emitting prompt): {unclassified}"
    )
    assert not stale, f"Allowlist entries with no call site any more: {stale}"

    mismatched = {
        site: (_SPAWN_CREATE_IMPL_ALLOWLIST[site], kind)
        for site, (kind, _call) in scanned.items()
        if _SPAWN_CREATE_IMPL_ALLOWLIST.get(site) != kind
    }
    assert not mismatched, (
        f"headless= shape changed at {mismatched} (allowlisted, scanned)"
    )


def test_headless_true_sites_use_sentinel_prompts() -> None:
    """Every ``headless=True`` caller must dispatch a sentinel-contract prompt."""
    true_sites = {
        site: call
        for site, (kind, call) in _scan_call_sites().items()
        if kind == "const-True"
    }
    assert true_sites, "expected at least the executor's headless=True call"
    for site, call in true_sites.items():
        prompt = _keyword(call, "prompt")
        assert isinstance(prompt, ast.JoinedStr), (
            f"{site}: headless=True requires an f-string /auto-dev ... --headless "
            "prompt that emits the AUTO_DEV_RESULT sentinel"
        )
        literals = [
            part.value
            for part in prompt.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        assert literals, f"{site}: prompt f-string has no literal parts"
        assert literals[0].startswith(_SENTINEL_PROMPT_PREFIX), (
            f"{site}: headless=True prompt must start with {_SENTINEL_PROMPT_PREFIX}"
        )
        assert any(_SENTINEL_PROMPT_FLAG in lit for lit in literals), (
            f"{site}: headless=True prompt must carry {_SENTINEL_PROMPT_FLAG}"
        )


def test_address_review_dispatch_is_not_headless() -> None:
    """``/address-review`` emits no sentinel, so it must not be headless (#2031)."""
    kind, _call = _scan_call_sites()[(_ADDRESS_REVIEW, "_dispatch_address_review")]
    assert kind == "const-False"
