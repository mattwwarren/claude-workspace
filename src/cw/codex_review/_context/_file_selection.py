"""Changed-file categorization and the reviewer-role selection tables.

Encodes ``/review`` Step 2's file-category flags and the small-/large-scope
reviewer-selection tables from ``auto-dev-review.md`` Step 3a and ``review.md``
Steps 2-3. Depends on the changed-path list and the caller's measured
``large_diff`` flag (#2086), which also selects the large-scope list and adds
the Transitive Impact lane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.codex_review._context._agent_spec import TRANSITIVE_IMPACT_REVIEWER_ROLE

if TYPE_CHECKING:
    from collections.abc import Iterable


class _FileCategories(NamedTuple):
    """Boolean file-category flags for reviewer selection (per /review Step 2)."""

    python: bool
    frontend: bool
    tests: bool
    infra: bool
    config: bool


def is_test_path(path: str) -> bool:
    """Whether *path* is a test file (the /review Step 2 ``tests`` rule).

    Shared with the codex fix loop's growth and constraint guards (#2633),
    which exclude test files from what a cycle is held to.
    """
    base = path.rsplit("/", 1)[-1]
    return (
        base.startswith("test_")
        or "_test." in base
        or path.startswith("tests/")
        or "/tests/" in path
        or "__tests__/" in path
    )


def _categorize_changed_files(files: Iterable[str]) -> _FileCategories:
    """Classify *files* into the /review Step 2 category flags."""
    python = frontend = tests = infra = config = False
    for path in files:
        base = path.rsplit("/", 1)[-1]
        if path.endswith(".py"):
            python = True
        if path.endswith((".ts", ".tsx", ".js", ".jsx", ".css")):
            frontend = True
        if is_test_path(path):
            tests = True
        if (
            base.startswith("Dockerfile")
            or path.endswith((".yaml", ".yml"))
            or ".github/" in path
            or path.startswith("k8s/")
        ):
            infra = True
        if path.endswith((".toml", ".cfg", ".ini")) or (
            path.endswith(".json") and base != "package.json"
        ):
            config = True
    return _FileCategories(python, frontend, tests, infra, config)


def _full_scope_roles(categories: _FileCategories, *, large_diff: bool) -> list[str]:
    """The "Large scope" list's file-category roles (review.md Steps 2-3).

    Plus the Transitive Impact lane (#2086), only when the diff is
    ``large_diff`` and code changed, placed after Deployment so the
    conditional Data Safety / Product Manager tail stays last.
    """
    roles: list[str] = []
    code_changed = categories.python or categories.frontend
    if code_changed:
        roles.append("Code Quality Reviewer")
    roles.append("SysAdmin Reviewer")
    if code_changed:
        roles.append("Architecture Reviewer")
    # review.md's table reads "Test files changed OR testable code
    # changed without test changes". Boolean-equivalent to `tests or
    # code_changed` by absorption (A or (B and not A) == A or B) — the
    # "without test changes" qualifier never changes the outcome, so it
    # is adopted as a simplification rather than encoded as dead-weight
    # `and not categories.tests` clauses (SHOULD_FIX 12, #1236).
    if categories.tests or code_changed:
        roles.append("Test Reviewer")
    if categories.python:
        roles.append("Performance Reviewer")
    if categories.python and categories.frontend:
        roles.append("API Contract Validator")
    if categories.infra:
        roles.append("Deployment Reviewer")
    if large_diff and code_changed:
        roles.append(TRANSITIVE_IMPACT_REVIEWER_ROLE)
    return roles


def _select_reviewer_roles(
    scope_tier: str,
    *,
    categories: _FileCategories,
    mutates_persisted_state: bool,
    has_ticket_context: bool,
    large_diff: bool,
) -> list[str]:
    """Select ordered reviewer roles, mandatory-first (Comment 3).

    Encodes the small-scope table (auto-dev-review.md Step 3a) and the
    large-scope file-category table (review.md Steps 2-3) verbatim.

    The large-scope list runs when the tier is ``large`` OR the diff is
    *large_diff* (#2086), mirroring auto-dev-review.md's "Reviewer-set
    selection"; this is the one decision point both review paths cite.
    *large_diff* is required so no caller silently drops the measurement.
    The Transitive Impact lane needs *large_diff* itself: a Large-tier diff
    that is not ``large_diff`` does not get it.

    ``categories.config`` is intentionally not a direct branch condition here:
    review.md's file-category table never assigns config its own reviewer row
    (unlike infra -> Deployment or python -> Performance) — its only defined
    effect is the Data Safety "skip on doc/config/style-only diffs" rule,
    which is already satisfied because a config-only diff also has
    ``python=False`` and ``frontend=False`` and therefore never sets
    ``mutates_persisted_state`` (see ``run_review``'s Adopted Assumption 2).
    ``categories.config`` remains a real, tested field of ``_FileCategories``
    for that categorization contract; it is simply a no-op input here.
    """
    full = scope_tier == "large" or large_diff
    if full:
        roles = _full_scope_roles(categories, large_diff=large_diff)
    else:
        roles = ["Code Quality Reviewer", "SysAdmin Reviewer"]
    if mutates_persisted_state:
        roles.append("Data Safety Reviewer")
    if has_ticket_context:
        roles.append("Product Manager Reviewer")
    return roles
