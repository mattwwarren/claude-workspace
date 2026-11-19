"""Guard: no new backend-name branch or ``TrackerClass`` read in ``src/`` (#3744).

RFC 0019 H2b moves "which harness is this, and can it reach its tickets
binding" behind the capability declarations and the binding probe. A new
``if backend == CODEX_BACKEND`` (or a new ``TrackerClass`` read) outside the
preflight paths is the shape this guard exists to stop: it reintroduces a
per-harness branch the declarations were meant to replace.

Two collectors scan every ``src/cw/**/*.py`` file with :mod:`ast`:

- **comparison sites**: an ``ast.Compare`` with an ``==``/``!=``/``in``/
  ``not in``/``is``/``is not`` op where any side is a ``*_BACKEND`` name or
  attribute, a backend-name string literal, a ``Harness.*`` attribute, or a
  tuple/set/list holding one;
- **TrackerClass sites**: any attribute read on the ``TrackerClass`` name.

Every site outside :data:`_EXEMPT_PREFIXES` must be listed in
:data:`_ALLOWLIST` with its exact per-file count. A count that grows fails
(new branch), and a count that shrinks fails too (stale entry: lower it). The
total is capped by :data:`_ALLOWLIST_CEILING`, so growth needs a visible edit.

Blind spots (not detected): dict or registry dispatch
(``{CODEX_BACKEND: ...}[backend]``), ``match`` statements, comparisons through
an intermediate variable, ``getattr``, and comparisons of non-literal backend
values. Extensibility: X1 (#3713) adds new names to :data:`_BACKEND_LITERALS`
and the detection tuples rather than a second guard.
"""

from __future__ import annotations

import ast
from collections import Counter
from typing import TYPE_CHECKING, Final

from tests._shared.workflow_text import _SRC_ROOT, _iter_src_files

if TYPE_CHECKING:
    from pathlib import Path

_BACKEND_LITERALS: Final = frozenset({"claude-native", "local", "codex", "opencode"})
_COMPARE_OPS: Final = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot)
_HARNESS: Final = "Harness"
_TRACKER_CLASS: Final = "TrackerClass"

#: Paths (relative to ``src/``) where backend branches are the point.
_EXEMPT_PREFIXES: Final = frozenset(
    {
        "cw/executor/",
        "cw/codex_finalize/",
        "cw/codex_fix_loop/",
        "cw/codex_impl/",
        "cw/codex_plan/",
        "cw/codex_review/",
        "cw/reconcile/codex_boot.py",
        "cw/reconcile/local.py",
        "cw/reconcile/harvest_synthesis.py",
        "cw/models/orchestrator_config/stage.py",
        "cw/tracker_io/",
        "cw/tracker.py",
        "cw/dispatch/claim/preflight_reach.py",
        "cw/binding_probe.py",
        "cw/binding_tools.py",
        "cw/stage_preflight/",
    }
)

#: Grandfathered comparison sites, ``{src-relative path: exact count}``.
_ALLOWLIST: Final[dict[str, int]] = {
    "cw/cli/review/converge.py": 1,
    "cw/codex_clean_confirm.py": 2,
    "cw/codex_confirm_feedback.py": 1,
    "cw/codex_legacy_recovery.py": 1,
    "cw/contract_manifest/_check.py": 1,
    "cw/contract_manifest/_stages.py": 1,
    "cw/dev_queue/requeue.py": 2,
    "cw/dispatch/gating/context_json.py": 1,
    "cw/doctor/codex_fix_loop.py": 1,
    "cw/doctor/review_convergence.py": 2,
    "cw/doctor/review_engine.py": 1,
    "cw/doctor/wedge/orphans.py": 1,
    "cw/executor_diagnostics.py": 1,
    "cw/models/bindings.py": 2,
    "cw/reconcile/unowned_running_acts.py": 1,
    "cw/review_convergence/_backend.py": 1,
}

#: Grandfathered ``TrackerClass`` sites, ``{src-relative path: exact count}``.
_TRACKER_ALLOWLIST: Final[dict[str, int]] = {
    "cw/codex_review_signals.py": 2,
    "cw/codex_stage_post.py": 1,
    "cw/dispatch/ticket_state_gate.py": 1,
}

#: Cap on the grandfathered total (both lists), so growth is a visible edit.
_ALLOWLIST_CEILING: Final = 24


def _is_backend_operand(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id.endswith("_BACKEND")
    if isinstance(node, ast.Attribute):
        if node.attr.endswith("_BACKEND"):
            return True
        return isinstance(node.value, ast.Name) and node.value.id == _HARNESS
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in _BACKEND_LITERALS
    if isinstance(node, ast.Tuple | ast.Set | ast.List):
        return any(_is_backend_operand(elt) for elt in node.elts)
    return False


def comparison_sites(tree: ast.AST) -> int:
    """Count backend-name comparisons in *tree*."""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, _COMPARE_OPS) for op in node.ops):
            continue
        if any(_is_backend_operand(side) for side in (node.left, *node.comparators)):
            count += 1
    return count


def tracker_class_sites(tree: ast.AST) -> int:
    """Count attribute reads on the ``TrackerClass`` name in *tree*."""
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == _TRACKER_CLASS
    )


def _rel(path: Path) -> str:
    return path.relative_to(_SRC_ROOT).as_posix()


def _exempt(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _scan() -> tuple[Counter[str], Counter[str]]:
    compares: Counter[str] = Counter()
    trackers: Counter[str] = Counter()
    for path in _iter_src_files():
        rel = _rel(path)
        if _exempt(rel):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if n := comparison_sites(tree):
            compares[rel] = n
        if n := tracker_class_sites(tree):
            trackers[rel] = n
    return compares, trackers


def _violations(found: Counter[str], allowed: dict[str, int]) -> list[str]:
    problems = [
        f"{rel}: {count} site(s), allowlist says {allowed.get(rel, 0)}"
        for rel, count in sorted(found.items())
        if count != allowed.get(rel, 0)
    ]
    problems += [
        f"{rel}: stale allowlist entry ({allowed[rel]}), file has none"
        for rel in sorted(allowed)
        if rel not in found
    ]
    return problems


def test_no_new_backend_name_comparison() -> None:
    compares, _ = _scan()
    assert _violations(compares, _ALLOWLIST) == []


def test_no_new_tracker_class_read() -> None:
    _, trackers = _scan()
    assert _violations(trackers, _TRACKER_ALLOWLIST) == []


def test_the_allowlist_total_is_capped() -> None:
    total = sum(_ALLOWLIST.values()) + sum(_TRACKER_ALLOWLIST.values())
    assert total <= _ALLOWLIST_CEILING


def test_no_allowlisted_file_is_also_exempt() -> None:
    assert [rel for rel in (*_ALLOWLIST, *_TRACKER_ALLOWLIST) if _exempt(rel)] == []


def test_the_exempt_prefix_set_is_pinned() -> None:
    """Widening the exempt set is a reviewable edit, never a quiet one."""
    assert sorted(_EXEMPT_PREFIXES) == [
        "cw/binding_probe.py",
        "cw/binding_tools.py",
        "cw/codex_finalize/",
        "cw/codex_fix_loop/",
        "cw/codex_impl/",
        "cw/codex_plan/",
        "cw/codex_review/",
        "cw/dispatch/claim/preflight_reach.py",
        "cw/executor/",
        "cw/models/orchestrator_config/stage.py",
        "cw/reconcile/codex_boot.py",
        "cw/reconcile/harvest_synthesis.py",
        "cw/reconcile/local.py",
        "cw/stage_preflight/",
        "cw/tracker.py",
        "cw/tracker_io/",
    ]


def test_the_scan_is_not_vacuous() -> None:
    compares, trackers = _scan()
    assert compares["cw/models/bindings.py"] == _ALLOWLIST["cw/models/bindings.py"]
    assert trackers["cw/codex_stage_post.py"] == 1


def test_the_collectors_flag_every_detected_shape() -> None:
    """Positive control: each operand shape counts once, a plain compare never."""
    source = "\n".join(
        [
            "a == CODEX_BACKEND",
            "cfg.backend != m.OPENCODE_BACKEND",
            "b in ('codex', 'x')",
            "h is Harness.LOCAL",
            "c in {LOCAL_BACKEND}",
            "x == 'claude-native'",
            "y == 'other'",
            "z < CODEX_BACKEND",
            "t = TrackerClass.GITHUB",
        ]
    )
    tree = ast.parse(source)
    assert comparison_sites(tree) == 6
    assert tracker_class_sites(tree) == 1


def test_an_unlisted_file_is_a_violation() -> None:
    assert _violations(Counter({"cw/new_module.py": 1}), {}) == [
        "cw/new_module.py: 1 site(s), allowlist says 0"
    ]
    assert _violations(Counter({"cw/a.py": 1}), {"cw/a.py": 2}) == [
        "cw/a.py: 1 site(s), allowlist says 2"
    ]
    assert _violations(Counter(), {"cw/gone.py": 1}) == [
        "cw/gone.py: stale allowlist entry (1), file has none"
    ]
