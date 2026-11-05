"""Seam assertions for the ``cw.gh`` package split (#2836).

The flat ``cw/gh.py`` passed CLAUDE.md's ~1000-line module ceiling. #2836
moves its code verbatim into eight submodules (``runner``, ``probes``,
``issues``, ``pr_reads``, ``merged``, ``branches``, ``pr_writes``,
``filing``) behind a re-exporting ``__init__``.

The behavioral suite (``tests/test_gh.py``) cannot see four things the move
must keep true:

1. The surface survives: every name reachable as ``cw.gh.<name>`` before the
   split still resolves there and is listed in ``__all__``.
2. Each name is *defined in* its owning submodule, and the package attribute
   is that object, so a patch on the owner reaches the real call site.
3. The patch seams live where the code reads them: intra-package callers look
   names up in their own submodule's globals, and ``_sp`` is the one global
   ``subprocess`` binding every ``gh`` spawn goes through.
4. The package stays split: every submodule is under the size flag and
   ``__init__`` is a pure re-export.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw.gh

if TYPE_CHECKING:
    from types import ModuleType

_RUNNER = "cw.gh.runner"
_PROBES = "cw.gh.probes"
_ISSUES = "cw.gh.issues"
_PR_READS = "cw.gh.pr_reads"
_MERGED = "cw.gh.merged"
_BRANCHES = "cw.gh.branches"
_PR_WRITES = "cw.gh.pr_writes"
_FILING = "cw.gh.filing"

_PACKAGE_DIR = Path(cw.gh.__file__).parent

# Owning submodule for every name on the package surface (62 names).
_OWNER: dict[str, str] = {
    "FETCH_COMMENTS_TIMEOUT": _RUNNER,
    "_POST_COMMENT_TIMEOUT_SECONDS": _RUNNER,
    "_run_gh_text": _RUNNER,
    "run_issue_view": _RUNNER,
    "run_pr_list": _RUNNER,
    "current_gh_login": _PROBES,
    "check_gh_availability": _PROBES,
    "fetch_repo_visibility": _PROBES,
    "_PLAN_MARKER": _ISSUES,
    "AGENT_COMMENT_MARKER": _ISSUES,
    "fetch_issue_comments": _ISSUES,
    "fetch_issue_body": _ISSUES,
    "_comment_has_marker": _ISSUES,
    "is_agent_authored": _ISSUES,
    "fetch_approved_plan_comment": _ISSUES,
    "post_issue_comment": _ISSUES,
    "agent_marked_body": _ISSUES,
    "_GH_PR_STATE_MERGED": _PR_READS,
    "_GH_PR_STATE_CLOSED": _PR_READS,
    "_GH_PR_STATE_OPEN": _PR_READS,
    "_PR_EXISTS_TIMEOUT": _PR_READS,
    "_PR_VIEW_TIMEOUT": _PR_READS,
    "_PR_VIEW_FIELDS": _PR_READS,
    "PR_ADOPT_FIELDS": _PR_READS,
    "_PR_HEAD_FIELDS": _PR_READS,
    "_BRANCH_PRS_LIMIT": _PR_READS,
    "github_pr_url": _PR_READS,
    "_fetch_pr_state": _PR_READS,
    "fetch_pr_view": _PR_READS,
    "BranchPr": _PR_READS,
    "_parse_branch_pr": _PR_READS,
    "fetch_branch_prs": _PR_READS,
    "_parse_branch_prs": _PR_READS,
    "PrHead": _PR_READS,
    "fetch_pr_head": _PR_READS,
    "_parse_pr_head": _PR_READS,
    "OpenPrLookup": _PR_READS,
    "_first_open_pr_url": _PR_READS,
    "pr_exists_for_branch": _PR_READS,
    "TIMED_OUT_MERGED_LOOKBACK_DAYS": _MERGED,
    "_fetch_issue_pr_refs": _MERGED,
    "_fetch_branch_merged_pr": _MERGED,
    "pr_is_merged_for_ticket": _MERGED,
    "pr_state_is_fresh": _MERGED,
    "pr_is_merged_from_state": _MERGED,
    "resolve_merged_via_pr_state": _MERGED,
    "_fetch_branch_exists_on_origin": _BRANCHES,
    "branch_exists_on_origin": _BRANCHES,
    "_fetch_branch_head_sha_on_origin": _BRANCHES,
    "branch_head_sha_on_origin": _BRANCHES,
    "add_pr_reviewer": _PR_WRITES,
    "disarm_pr_auto_merge": _PR_WRITES,
    "_CREATE_TIMEOUT": _FILING,
    "_ISSUE_URL_NUMBER_RE": _FILING,
    "_body_file": _FILING,
    "create_issue": _FILING,
    "_attach_milestone": _FILING,
    "update_issue_body": _FILING,
    "create_milestone": _FILING,
    "find_milestone": _FILING,
    "latest_release_tag": _FILING,
    "milestone_issue_titles": _FILING,
}

# The package surface: every name the flat module defined at top level, plus
# the ``_sp`` subprocess seam that tests patch through the package.
_SURFACE = frozenset(_OWNER) | {"_sp"}

# Module-level constants carry no ``__module__``; ownership is checked by
# membership in the owner's namespace instead.
_CONSTANTS = frozenset(
    {
        "FETCH_COMMENTS_TIMEOUT",
        "_POST_COMMENT_TIMEOUT_SECONDS",
        "_PLAN_MARKER",
        "AGENT_COMMENT_MARKER",
        "_GH_PR_STATE_MERGED",
        "_GH_PR_STATE_CLOSED",
        "_GH_PR_STATE_OPEN",
        "_PR_EXISTS_TIMEOUT",
        "_PR_VIEW_TIMEOUT",
        "_PR_VIEW_FIELDS",
        "PR_ADOPT_FIELDS",
        "_PR_HEAD_FIELDS",
        "_BRANCH_PRS_LIMIT",
        "TIMED_OUT_MERGED_LOOKBACK_DAYS",
        "_CREATE_TIMEOUT",
        "_ISSUE_URL_NUMBER_RE",
    }
)

# Submodules that spawn ``gh`` and must bind ``_sp`` to ``subprocess`` so a
# ``cw.gh._sp.run`` patch reaches their call sites.
_SPAWNING_MODULES = (
    _RUNNER,
    _PROBES,
    _ISSUES,
    _PR_READS,
    _MERGED,
    _BRANCHES,
    _PR_WRITES,
    _FILING,
)

# (patched name, consumer module that must bind it, reader functions in that
# consumer whose globals the patch has to reach).
_SEAMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("_run_gh_text", _RUNNER, ("run_issue_view", "run_pr_list")),
    ("current_gh_login", _ISSUES, ("fetch_approved_plan_comment",)),
    ("fetch_issue_comments", _ISSUES, ("fetch_approved_plan_comment",)),
    ("agent_marked_body", _ISSUES, ("post_issue_comment",)),
    ("fetch_pr_view", _PR_READS, ("fetch_pr_head",)),
    ("_parse_pr_head", _PR_READS, ("fetch_pr_head",)),
    ("_parse_branch_prs", _PR_READS, ("fetch_branch_prs",)),
    ("_first_open_pr_url", _PR_READS, ("pr_exists_for_branch",)),
    ("_fetch_pr_state", _MERGED, ("pr_is_merged_for_ticket",)),
    ("_fetch_issue_pr_refs", _MERGED, ("pr_is_merged_for_ticket",)),
    ("_fetch_branch_merged_pr", _MERGED, ("pr_is_merged_for_ticket",)),
    ("_GH_PR_STATE_MERGED", _MERGED, ("pr_is_merged_from_state",)),
    ("pr_state_is_fresh", _MERGED, ("resolve_merged_via_pr_state",)),
    ("pr_is_merged_from_state", _MERGED, ("resolve_merged_via_pr_state",)),
    (
        "_fetch_branch_exists_on_origin",
        _BRANCHES,
        ("branch_exists_on_origin",),
    ),
    (
        "_fetch_branch_head_sha_on_origin",
        _BRANCHES,
        ("branch_head_sha_on_origin",),
    ),
    ("_body_file", _FILING, ("create_issue", "update_issue_body")),
    ("_attach_milestone", _FILING, ("create_issue",)),
)

#: CLAUDE.md's "Module Size" yellow flag; the flat module was 1398 lines.
_MODULE_LINE_LIMIT = 800


def _owner(name: str) -> ModuleType:
    return importlib.import_module(_OWNER[name])


def _package_files() -> list[Path]:
    return sorted(_PACKAGE_DIR.glob("*.py")) if _PACKAGE_DIR.name == "gh" else []


# ---------------------------------------------------------------------------
# Surface: green on the flat module, stays green after the split
# ---------------------------------------------------------------------------


def test_owner_table_covers_the_surface() -> None:
    """The owner and constant tables describe the same names."""
    assert len(_OWNER) == 62
    assert _CONSTANTS.issubset(_OWNER)


@pytest.mark.parametrize("name", sorted(_SURFACE))
def test_surface_name_resolves_on_the_package(name: str) -> None:
    """Every pre-split name is still reachable as ``cw.gh.<name>``."""
    assert hasattr(cw.gh, name)


def test_sp_is_the_global_subprocess_seam() -> None:
    """``cw.gh._sp`` is ``subprocess``, so ``cw.gh._sp.run`` patches are global."""
    assert cw.gh._sp is subprocess


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_package_all_is_the_pinned_surface() -> None:
    """``__all__`` lists exactly the surface names, no more and no fewer."""
    assert sorted(getattr(cw.gh, "__all__", ())) == sorted(_SURFACE)


@pytest.mark.parametrize("name", sorted(_OWNER))
def test_package_attribute_is_the_owner_object(name: str) -> None:
    """The package attribute is the owner's object, not a copy."""
    assert getattr(cw.gh, name) is vars(_owner(name))[name]


@pytest.mark.parametrize("name", sorted(set(_OWNER) - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """Functions and classes report their owning submodule as ``__module__``."""
    obj = vars(_owner(name))[name]
    assert obj.__module__ == _OWNER[name]


@pytest.mark.parametrize("name", sorted(_CONSTANTS))
def test_constant_is_bound_in_its_owner(name: str) -> None:
    """Constants live in their owner's namespace."""
    assert name in vars(_owner(name))


@pytest.mark.parametrize("module", _SPAWNING_MODULES)
def test_spawning_submodule_binds_sp_to_subprocess(module: str) -> None:
    """Every submodule that spawns ``gh`` routes through the shared ``_sp``."""
    assert vars(importlib.import_module(module))["_sp"] is subprocess


@pytest.mark.parametrize(
    ("seam", "consumer", "readers"), _SEAMS, ids=[seam for seam, _, _ in _SEAMS]
)
def test_patch_seam_is_bound_where_it_is_read(
    seam: str, consumer: str, readers: tuple[str, ...]
) -> None:
    """The consumer binds the seam and its readers look it up in its globals."""
    module = importlib.import_module(consumer)
    assert seam in vars(module)
    for reader in readers:
        fn = vars(module)[reader]
        assert inspect.isfunction(fn)
        assert fn.__globals__ is vars(module)


# ---------------------------------------------------------------------------
# Final-state guards: module size and a pure ``__init__``
# ---------------------------------------------------------------------------


def test_package_files_are_under_the_module_size_flag() -> None:
    """``cw.gh`` is a package and no submodule nears the ceiling."""
    files = _package_files()
    assert files, "cw.gh is not a package"
    for path in files:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count < _MODULE_LINE_LIMIT, path.name


def test_package_init_is_a_pure_reexport() -> None:
    """``__init__`` binds only re-exports, its submodules and ``annotations``."""
    files = _package_files()
    assert files, "cw.gh is not a package"
    tree = ast.parse((_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8"))
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        )
    ]
    bound = {name for name in vars(cw.gh) if not name.startswith("__")}
    submodules = {path.stem for path in files} - {"__init__"}
    # ``annotations`` is the ``from __future__ import annotations`` binding.
    assert bound == set(_SURFACE) | submodules | {"annotations"}
