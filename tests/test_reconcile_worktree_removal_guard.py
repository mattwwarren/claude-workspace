"""Pin the worktree-mutating primitives under ``src/cw/reconcile/`` (#2630).

No reconcile act removes a worktree, with one scoped exception (#2771): the
post-lock ``repair_changelog_conflict`` job prunes and removes only the
detached scratch tree it created itself, through the single ``_remove_scratch``
helper, never the ticket's worktree and never under a lock; for the phantom
sweep and backstops the capture only selects the queue disposition (#1747,
ADR-0014). Post-lock surface stops do not read or depend on the dirt value (a
merged phantom's candidacy still requires a usable capture; a miss defers it).

What this guard pins, keyed by ``(module_relpath, token)`` with a count:

- calls to, and ``from ... import`` of, the removal names ``remove_worktree``,
  ``remove_worktree_gc``, ``run_worktree_gc``, ``done_session`` and ``rmtree``;
- git argv ``worktree remove`` / ``worktree prune``;
- as a neighbouring tripwire, git ``reset``, ``clean``, ``restore``,
  ``checkout`` and ``stash`` argv;
- the single ``create_worktree`` reuse-refresh call site (ff-only, never
  removes), so a second worktree-mutating entry point is a reviewed addition.
- the repair recipe's one ``git worktree prune`` and one ``git worktree
  remove`` call site, both in ``changelog_repair_git._remove_scratch``
  (#2771), so a second removal there is a reviewed addition too.

Deliberately NOT pinned: ``git merge`` (including the merge and the
``merge --abort`` in ``review_recipes/fix_agent.py``), ``branch -D`` and
``unlink``. Zero ``unlink`` / ``branch -D`` occurrences exist under
``src/cw/reconcile/`` today; ``merge`` relies on git's own overwrite refusal.

A tripwire, not a proof: a removal reached through a helper whose name is
outside the set is not seen, and ``cw.worktree`` is not a pinned surface. Scope
is ``src/cw/reconcile/``; ``cw/doctor/``, ``session.py``, ``dispatch/`` and
``worktree_gc.py`` are enumerated by whole-``src/cw`` grep, not pinned here.

The unavailable-helper (fail-open) direction is tracked in #2662; the GC
instance (``cw worktree gc --apply``) is tracked in #2669.
"""

from __future__ import annotations

import ast
from collections import Counter
from itertools import pairwise

from tests.conftest import _SRC_ROOT, _files_under

_RECONCILE_PREFIX = "cw/reconcile/"

_REMOVAL_CALLEES: frozenset[str] = frozenset(
    {
        "remove_worktree",
        "remove_worktree_gc",
        "run_worktree_gc",
        "done_session",
        "rmtree",
    }
)
_WORKTREE_SUBCOMMANDS: frozenset[str] = frozenset({"remove", "prune"})
_GIT_VERBS: tuple[str, ...] = ("reset", "clean", "restore", "checkout", "stash")
_CREATE_WORKTREE = "create_worktree"
_GIT = "git"

_ALLOWLIST: dict[tuple[str, str], int] = {
    # Why: ff-only reuse refresh with a fresh occupancy re-check immediately
    # before the merge (#2213); never removes.
    ("cw/reconcile/review_recipes/fix_agent.py", _CREATE_WORKTREE): 1,
    # Why: the repair recipe's own detached scratch tree
    # (<worktree_base>/.changelog-repair-<ticket>), created by the recipe in the
    # same dispatch and never the ticket worktree, is pruned and removed best
    # effort by one helper, _remove_scratch, called from the pre-clean and the
    # finally (#2771). Post-lock; no lock held.
    ("cw/reconcile/review_recipes/changelog_repair_git.py", "git:worktree prune"): 1,
    ("cw/reconcile/review_recipes/changelog_repair_git.py", "git:worktree remove"): 1,
}


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _str_constants(nodes: list[ast.expr]) -> list[str]:
    return [
        node.value
        for node in nodes
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _is_git_list(node: ast.expr) -> bool:
    if not isinstance(node, ast.List) or not node.elts:
        return False
    first = node.elts[0]
    return isinstance(first, ast.Constant) and first.value == _GIT


def _argv_keys(tokens: list[str]) -> list[str]:
    """``git:worktree remove|prune`` for adjacent tokens, ``git:<verb>`` per verb."""
    keys = [
        f"git:worktree {nxt}"
        for cur, nxt in pairwise(tokens)
        if cur == "worktree" and nxt in _WORKTREE_SUBCOMMANDS
    ]
    keys.extend(f"git:{verb}" for verb in _GIT_VERBS if verb in tokens)
    return keys


def _removal_call_key(call: ast.Call) -> list[str]:
    name = _callee_name(call)
    return [name] if name in _REMOVAL_CALLEES else []


def _import_keys(node: ast.ImportFrom) -> list[str]:
    return [f"import:{a.name}" for a in node.names if a.name in _REMOVAL_CALLEES]


def _git_call_keys(call: ast.Call) -> list[str]:
    """Argv keys of a git-runner call; a ``["git", ...]`` list arg is skipped
    because the list arm owns it, so it counts once."""
    name = _callee_name(call)
    if name is None or _GIT not in name:
        return []
    tokens: list[str] = []
    for arg in call.args:
        if isinstance(arg, ast.List | ast.Tuple):
            if not _is_git_list(arg):
                tokens.extend(_str_constants(arg.elts))
        else:
            tokens.extend(_str_constants([arg]))
    return _argv_keys(tokens)


def _git_list_keys(node: ast.List) -> list[str]:
    return _argv_keys(_str_constants(node.elts)) if _is_git_list(node) else []


def _create_worktree_key(call: ast.Call) -> list[str]:
    return [_CREATE_WORKTREE] if _callee_name(call) == _CREATE_WORKTREE else []


def _node_keys(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Call):
        return [
            *_removal_call_key(node),
            *_git_call_keys(node),
            *_create_worktree_key(node),
        ]
    if isinstance(node, ast.ImportFrom):
        return _import_keys(node)
    if isinstance(node, ast.List):
        return _git_list_keys(node)
    return []


def _count_mutations(source: str, relpath: str) -> dict[tuple[str, str], int]:
    tree = ast.parse(source, filename=relpath)
    counts = Counter(key for node in ast.walk(tree) for key in _node_keys(node))
    return {(relpath, key): n for key, n in counts.items()}


def _scan_reconcile_mutations() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in _files_under(_RECONCILE_PREFIX):
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        found.update(_count_mutations(path.read_text(encoding="utf-8"), relpath))
    return found


def test_scan_covers_the_reconcile_package() -> None:
    relpaths = {
        p.relative_to(_SRC_ROOT).as_posix() for p in _files_under(_RECONCILE_PREFIX)
    }

    assert "cw/reconcile/core.py" in relpaths
    assert "cw/reconcile/phantom/_events.py" in relpaths
    assert not any(not r.startswith(_RECONCILE_PREFIX) for r in relpaths)


def test_reconcile_has_no_worktree_removal_call_sites() -> None:
    scanned = _scan_reconcile_mutations()

    assert scanned == _ALLOWLIST, (
        "A worktree-mutating primitive changed under src/cw/reconcile/ (a "
        "removal call or import, a git worktree remove/prune or "
        "reset/clean/restore/checkout/stash argv, or the pinned create_worktree "
        "count). No reconcile act removes a worktree, with one scoped exception "
        "(#2771): the post-lock repair_changelog_conflict job prunes and removes "
        "only its own detached scratch tree via _remove_scratch, never the "
        "ticket's worktree and never under a lock; for the phantom sweep and "
        "backstops the capture only selects the queue disposition (ADR-0014, "
        "ADR-0006 Amendment #2630). A removal decided inside reconcile() would "
        "consume a pre-lock capture up to DIRTY_CHECK_MAX_AGE_SECONDS old "
        "(#2548) and must re-verify fresh immediately before acting. See #2662 "
        f"for the helper's fail-open direction. Scanned: {scanned}"
    )


def test_collector_flags_removal_calls_imports_and_git_argv() -> None:
    cases = {
        "remove_worktree(c, b, force=True)\n": "remove_worktree",
        "shutil.rmtree(p)\n": "rmtree",
        "from cw.worktree import remove_worktree as _rw\n": "import:remove_worktree",
        '_run_git("worktree", "remove", p, cwd=w)\n': "git:worktree remove",
        '["git", "worktree", "remove", "--force", p]\n': "git:worktree remove",
        'run_git(["reset", "--hard"], cwd=w)\n': "git:reset",
        '_git(repo, "worktree", "prune", timeout=60)\n': "git:worktree prune",
    }

    for source, token in cases.items():
        assert _count_mutations(source, "x.py") == {("x.py", token): 1}, source


def test_collector_ignores_non_mutating_sources() -> None:
    sources = [
        '["git", "status", "--porcelain"]\n',
        'report = {"clean": report.clean}\n',
        '"""Mentions the worktree in a docstring."""\nlabel = "worktree"\n',
        "from cw.worktree import create_worktree\n",
        '_run_git("merge", "--abort", cwd=worktree, check=False)\n',
    ]

    for source in sources:
        assert _count_mutations(source, "x.py") == {}, source
