"""Pin every ``.stop(`` call under ``src/cw/reconcile/`` to an allowlist (#1232).

A daemon surface stop is an external call (``claude stop``, up to 10s). Inside
``reconcile()`` it must not run under ``sessions_lock``: act phases queue it on
the post-lock sink (``cw.reconcile.deferred``) instead, and the one remaining
in-lock stop is the mid-turn usage-limit act's own, whose stop/persist split is
tracked in #2549. So the reconcile package may call ``.stop(`` in exactly these
places, keyed by ``(module_relpath, qualified_function_name)`` with a count:

- ``cw/reconcile/deferred.py::_run_surface_stop`` -- the queued surface stop,
  run from the post-lock drain.
- ``cw/reconcile/leaked_workers.py::stop_leaked_daemon_worker`` -- the
  leaked-worker stop-and-audit, queued by ``reconcile()`` and run inline only by
  the lock-free ``cw doctor --reap`` path.
- ``cw/reconcile/usage_limit_mid_turn.py::_stop_surface`` -- the in-lock
  exception (#2549).

The scan compares the full ``{site: count}`` mapping, so a new call site, a
second call in an allowlisted function, or a stale entry all fail
:func:`test_reconcile_stop_call_sites_match_allowlist`. Qualified names follow
CPython's ``__qualname__`` convention, mirroring
``tests/test_ambiguous_session_lookup_guard.py``.
"""

from __future__ import annotations

import ast
from collections import Counter
from typing import TYPE_CHECKING

from tests.conftest import _SRC_ROOT, _iter_src_files

if TYPE_CHECKING:
    from pathlib import Path

_STOP_ATTR = "stop"
_RECONCILE_PREFIX = "cw/reconcile/"

_STOP_CALL_ALLOWLIST: dict[tuple[str, str], int] = {
    ("cw/reconcile/deferred.py", "_run_surface_stop"): 1,
    ("cw/reconcile/leaked_workers.py", "stop_leaked_daemon_worker"): 1,
    # Why: the one in-lock reconcile stop; split tracked in #2549.
    ("cw/reconcile/usage_limit_mid_turn.py", "_stop_surface"): 1,
}


class _StopCallCollector(ast.NodeVisitor):
    """Count ``<expr>.stop(...)`` calls per enclosing qualified name."""

    def __init__(self) -> None:
        # (kind, name) per enclosing scope; kind is "function" or "class".
        self._stack: list[tuple[str, str]] = []
        self.counts: Counter[str] = Counter()

    def _qualname(self) -> str:
        parts: list[str] = []
        for index, (kind, name) in enumerate(self._stack):
            parts.append(name)
            if kind == "function" and index < len(self._stack) - 1:
                parts.append("<locals>")
        return ".".join(parts) or "<module>"

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
        if isinstance(node.func, ast.Attribute) and node.func.attr == _STOP_ATTR:
            self.counts[self._qualname()] += 1
        self.generic_visit(node)


def _count_stop_calls(source: str, relpath: str) -> dict[tuple[str, str], int]:
    collector = _StopCallCollector()
    collector.visit(ast.parse(source, filename=relpath))
    return {(relpath, qualname): n for qualname, n in collector.counts.items()}


def _reconcile_files() -> list[Path]:
    return [
        path
        for path in _iter_src_files()
        if path.relative_to(_SRC_ROOT).as_posix().startswith(_RECONCILE_PREFIX)
    ]


def _scan_reconcile_stop_calls() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in _reconcile_files():
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        found.update(_count_stop_calls(path.read_text(encoding="utf-8"), relpath))
    return found


def test_scan_covers_the_reconcile_package() -> None:
    relpaths = {p.relative_to(_SRC_ROOT).as_posix() for p in _reconcile_files()}

    assert "cw/reconcile/core.py" in relpaths
    assert "cw/reconcile/phantom/_events.py" in relpaths
    assert not any(not r.startswith(_RECONCILE_PREFIX) for r in relpaths)


def test_reconcile_stop_call_sites_match_allowlist() -> None:
    scanned = _scan_reconcile_stop_calls()

    assert scanned == _STOP_CALL_ALLOWLIST, (
        "A .stop( call under src/cw/reconcile/ changed. A daemon stop decided "
        "inside reconcile() must be queued with "
        "cw.reconcile.deferred.defer_surface_stop, not called under "
        f"sessions_lock. Scanned: {scanned}"
    )


def test_collector_flags_an_extra_call_in_an_allowlisted_function() -> None:
    """A second stop in an allowlisted function changes its count, and a stop
    in a nested helper or method is keyed by its own qualified name."""
    source = (
        "def _stop_surface(act):\n"
        "    daemon.stop(act.ref)\n"
        "    daemon.stop(act.other)\n"
        "\n"
        "def outer():\n"
        "    def inner():\n"
        "        client().stop('x')\n"
        "    return inner\n"
        "\n"
        "class Holder:\n"
        "    def method(self):\n"
        "        self.daemon.stop('y')\n"
        "\n"
        "stopper.stop('module-level')\n"
        "not_a_stop('z')\n"
    )
    relpath = "cw/reconcile/usage_limit_mid_turn.py"

    counts = _count_stop_calls(source, relpath)

    assert counts == {
        (relpath, "_stop_surface"): 2,
        (relpath, "outer.<locals>.inner"): 1,
        (relpath, "Holder.method"): 1,
        (relpath, "<module>"): 1,
    }
    assert counts != {
        key: n for key, n in _STOP_CALL_ALLOWLIST.items() if key[0] == relpath
    }
