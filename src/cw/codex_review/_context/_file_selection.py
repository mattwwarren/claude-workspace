"""Changed-file categorization and the reviewer-role selection tables.

Encodes ``/review`` Step 2's file-category flags and the small-/large-scope
reviewer-selection tables from ``auto-dev-review.md`` Step 3a and ``review.md``
Steps 2-3. Depends on nothing but the changed-path list itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable


class _FileCategories(NamedTuple):
    """Boolean file-category flags for reviewer selection (per /review Step 2)."""

    python: bool
    frontend: bool
    tests: bool
    infra: bool
    config: bool


def _categorize_changed_files(files: Iterable[str]) -> _FileCategories:
    """Classify *files* into the /review Step 2 category flags."""
    python = frontend = tests = infra = config = False
    for path in files:
        base = path.rsplit("/", 1)[-1]
        if path.endswith(".py"):
            python = True
        if path.endswith((".ts", ".tsx", ".js", ".jsx", ".css")):
            frontend = True
        if (
            base.startswith("test_")
            or "_test." in base
            or path.startswith("tests/")
            or "/tests/" in path
            or "__tests__/" in path
        ):
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


def _select_reviewer_roles(
    scope_tier: str,
    *,
    categories: _FileCategories,
    mutates_persisted_state: bool,
    has_ticket_context: bool,
) -> list[str]:
    """Select ordered reviewer roles, mandatory-first (Comment 3).

    Encodes the small-scope table (auto-dev-review.md Step 3a) and the
    large-scope file-category table (review.md Steps 2-3) verbatim.

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
    roles: list[str] = []
    code_changed = categories.python or categories.frontend
    if scope_tier == "large":
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
        if categories.tests or categories.python or categories.frontend:
            roles.append("Test Reviewer")
        if categories.python:
            roles.append("Performance Reviewer")
        if categories.python and categories.frontend:
            roles.append("API Contract Validator")
        if categories.infra:
            roles.append("Deployment Reviewer")
    else:
        roles.append("Code Quality Reviewer")
        roles.append("SysAdmin Reviewer")
    if mutates_persisted_state:
        roles.append("Data Safety Reviewer")
    if has_ticket_context:
        roles.append("Product Manager Reviewer")
    return roles
