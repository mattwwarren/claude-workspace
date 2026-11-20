"""Guard: no new backend-name or tracker-identity comparison in ``src/`` (#3744, #3713).

RFC 0019 H2b moves "which harness is this, and can it reach its tickets
binding" behind the capability declarations and the binding probe. A new
``if backend == CODEX_BACKEND`` (or a new tracker-identity comparison) outside
the preflight paths is the shape this guard exists to stop: it reintroduces a
per-harness or per-tracker branch the declarations were meant to replace.

Two collectors scan every ``src/cw/**/*.py`` file with :mod:`ast`:

- **backend comparison sites**: an ``ast.Compare`` with an ``==``/``!=``/``in``/
  ``not in``/``is``/``is not`` op where any side is a ``*_BACKEND`` name or
  attribute, a backend-name string literal, a ``Harness.*`` attribute, or a
  tuple/set/list holding one;
- **tracker comparison sites**: the same ops where any side is a
  ``TRACKER_GITHUB_ISSUES``/``TRACKER_LINEAR`` name or attribute, a
  ``"github-issues"``/``"linear"`` string literal, or a tuple/set/list holding
  one. Introduced by X1 (#3713), which retired ``TrackerClass``.

Every backend site outside :data:`_EXEMPT_PREFIXES` must be listed in
:data:`_ALLOWLIST` with its exact per-file count. A count that grows fails
(new branch), and a count that shrinks fails too (stale entry: lower it). The
backend total is capped by :data:`_ALLOWLIST_CEILING`.

Tracker sites are scanned in every file, with no exemption set. Each one is
listed in :data:`_TRACKER_ALLOWLIST` as ``(count, reason)``, and the tracker
list has its own cap, :data:`_TRACKER_ALLOWLIST_CEILING`. That list is separate
from the backend list, was introduced by X1 (#3713) and may only shrink.

Blind spots (not detected, for both collectors): comparison through an
intermediate variable, ``in`` against a named tuple or frozenset, dict or
registry dispatch (``{CODEX_BACKEND: ...}[backend]``), ``match`` statements,
``getattr``, and comparisons of non-literal values.
"""

from __future__ import annotations

import ast
from collections import Counter
from typing import TYPE_CHECKING, Final

import pytest

from tests._shared.workflow_text import _SRC_ROOT, _iter_src_files

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_BACKEND_LITERALS: Final = frozenset({"claude-native", "local", "codex", "opencode"})
_TRACKER_NAMES: Final = frozenset({"TRACKER_LINEAR", "TRACKER_GITHUB_ISSUES"})
_TRACKER_LITERALS: Final = frozenset({"linear", "github-issues"})
_COMPARE_OPS: Final = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot)
_HARNESS: Final = "Harness"

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

#: Grandfathered backend comparison sites, ``{src-relative path: exact count}``.
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

#: Grandfathered tracker identity sites, ``{src-relative path: (count, reason)}``.
#: Each reason is one line saying why the file reads tracker identity.
_TRACKER_ALLOWLIST: Final[dict[str, tuple[int, str]]] = {
    "cw/adopt_pr.py": (
        1,
        "adopt-PR refuses a non-GitHub tracker; PR-to-ticket linkage is GitHub-only",
    ),
    "cw/bindings_block.py": (
        3,
        "binding-first effective tickets system picks the gh versus MCP prompt block",
    ),
    "cw/cli/review/_settle_store.py": (
        1,
        "settle store: GitHub reads marker comments, other trackers use the cw store",
    ),
    "cw/cli/ticket.py": (1, "no-binding fallback: a GitHub repo reads through gh"),
    "cw/codex_plan/_run.py": (
        1,
        "binding-first effective system: GitHub needs no MCP target",
    ),
    "cw/dev_queue/lifecycle.py": (
        1,
        "plan fetch is GitHub-only: unresolved or GitHub may fetch, a known other "
        "tracker may not",
    ),
    "cw/dev_queue/requeue.py": (
        2,
        "impl-bypass plan fetch (line 189) and review-reentry delivery "
        "(line 481; follow-up debt)",
    ),
    "cw/dispatch/claim/preflight_reach.py": (
        1,
        "GitHub needs no MCP binding; unset fails open",
    ),
    "cw/dispatch/ticket_state_gate.py": (
        1,
        "closed-ticket state is read only on GitHub (ADR-0013)",
    ),
    "cw/doctor/config_checks.py": (
        1,
        "doctor cross-check on raw YAML (line 85); kept raw, not migrated",
    ),
    "cw/outbox_drain.py": (
        1,
        "no-bindings fallback: unset or GitHub drains through gh",
    ),
    "cw/preflight.py": (
        4,
        "cw preflight tracker recognition (line 161) and GitHub versus other "
        "ticket-id and PR-query shape (331, 393, 429); unset defaults to linear",
    ),
    "cw/review_debt_filing/_link.py": (
        1,
        "only GitHub-filed debt records are linkable (numeric ids)",
    ),
    "cw/review_debt_filing/_tracker.py": (
        2,
        "tracker_mode: strict github-issues or linear polarity, else skip",
    ),
    "cw/ticket_snapshot/_refresh.py": (
        1,
        "is_github: snapshot refresh path is GitHub-only",
    ),
}

#: Cap on the grandfathered backend total, so growth is a visible edit.
_ALLOWLIST_CEILING: Final = 24

#: Cap on the grandfathered tracker total, introduced by X1 (#3713); may only shrink.
_TRACKER_ALLOWLIST_CEILING: Final = 22


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


def _is_tracker_operand(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _TRACKER_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _TRACKER_NAMES
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in _TRACKER_LITERALS
    if isinstance(node, ast.Tuple | ast.Set | ast.List):
        return any(_is_tracker_operand(elt) for elt in node.elts)
    return False


def _count_compares(tree: ast.AST, is_operand: Callable[[ast.expr], bool]) -> int:
    """Count comparisons in *tree* with a sided operand *is_operand* accepts."""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, _COMPARE_OPS) for op in node.ops):
            continue
        if any(is_operand(side) for side in (node.left, *node.comparators)):
            count += 1
    return count


def comparison_sites(tree: ast.AST) -> int:
    """Count backend-name comparisons in *tree*."""
    return _count_compares(tree, _is_backend_operand)


def tracker_comparison_sites(tree: ast.AST) -> int:
    """Count tracker-identity comparisons in *tree*."""
    return _count_compares(tree, _is_tracker_operand)


def _rel(path: Path) -> str:
    return path.relative_to(_SRC_ROOT).as_posix()


def _exempt(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _scan() -> tuple[Counter[str], Counter[str]]:
    """Backend sites skip the exempt prefixes; tracker sites scan every file."""
    compares: Counter[str] = Counter()
    trackers: Counter[str] = Counter()
    for path in _iter_src_files():
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _exempt(rel) and (n := comparison_sites(tree)):
            compares[rel] = n
        if n := tracker_comparison_sites(tree):
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


def _tracker_counts() -> dict[str, int]:
    return {rel: entry[0] for rel, entry in _TRACKER_ALLOWLIST.items()}


def test_no_new_backend_name_comparison() -> None:
    compares, _ = _scan()
    assert _violations(compares, _ALLOWLIST) == []


def test_no_new_tracker_comparison() -> None:
    _, trackers = _scan()
    assert _violations(trackers, _tracker_counts()) == []


def test_the_backend_allowlist_total_is_capped() -> None:
    assert sum(_ALLOWLIST.values()) <= _ALLOWLIST_CEILING


def test_the_tracker_allowlist_total_is_capped() -> None:
    assert sum(_tracker_counts().values()) <= _TRACKER_ALLOWLIST_CEILING


def test_no_allowlisted_file_is_also_exempt() -> None:
    assert [rel for rel in _ALLOWLIST if _exempt(rel)] == []


def test_every_tracker_reason_is_one_non_empty_line() -> None:
    for rel, (_, reason) in _TRACKER_ALLOWLIST.items():
        assert reason.strip(), rel
        assert "\n" not in reason, rel


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
    compares, _ = _scan()
    assert compares["cw/models/bindings.py"] == _ALLOWLIST["cw/models/bindings.py"]


@pytest.mark.parametrize("rel", sorted(_TRACKER_ALLOWLIST))
def test_each_tracker_entry_matches_a_real_site(rel: str) -> None:
    """Per-entry vacuity control: every listed tracker file really has its count."""
    _, trackers = _scan()
    count, _ = _TRACKER_ALLOWLIST[rel]
    assert trackers[rel] == count
    assert count > 0


def test_the_collectors_flag_every_detected_shape() -> None:
    """Positive control: each backend operand shape counts once."""
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
        ]
    )
    tree = ast.parse(source)
    assert comparison_sites(tree) == 6
    assert tracker_comparison_sites(tree) == 0


def test_the_tracker_collector_flags_every_detected_shape() -> None:
    """Positive control for tracker identity: six shapes count once, the rest never."""
    source = "\n".join(
        [
            "a == TRACKER_LINEAR",
            "b != m.TRACKER_GITHUB_ISSUES",
            "c in ('linear', 'x')",
            "d is not TRACKER_LINEAR",
            "e not in {TRACKER_GITHUB_ISSUES}",
            "f == 'github-issues'",
            "g == 'other'",
            "h < TRACKER_LINEAR",
            "x = TRACKER_LINEAR",
            "{TRACKER_LINEAR: 1}[k]",
            "f(TRACKER_LINEAR)",
        ]
    )
    tree = ast.parse(source)
    assert tracker_comparison_sites(tree) == 6
    assert comparison_sites(tree) == 0


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
