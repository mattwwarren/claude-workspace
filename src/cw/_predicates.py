"""Dependency-free value predicates shared across cw's layers.

Imports nothing from ``cw``, so both the persisted-model layer and the
sentinel schema layer can depend on it without a cycle. Lives outside
``cw.models.enums`` because a generic predicate is not an enum concern.
"""

from __future__ import annotations

import re
from typing import Final, TypeGuard

REPO_SLUG_PATTERN: Final = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
"""A github ``owner/repo`` slug (the shape of ``cw.gh.probes._REPO_SLUG``)."""

_REPO_SLUG_RE: Final = re.compile(REPO_SLUG_PATTERN)
_REPO_PR_REF_RE: Final = re.compile(rf"({REPO_SLUG_PATTERN})#([1-9][0-9]*)")
_DOT_SEGMENTS: Final = frozenset({".", ".."})


def is_positive_real_int(value: object) -> TypeGuard[int]:
    """Return whether *value* is a positive, non-boolean integer."""
    return type(value) is int and value > 0


def is_repo_slug(value: object) -> TypeGuard[str]:
    """Return whether *value* is an ``owner/repo`` slug with no ``.``/``..`` part."""
    if not isinstance(value, str) or _REPO_SLUG_RE.fullmatch(value) is None:
        return False
    return not any(part in _DOT_SEGMENTS for part in value.split("/"))


def parse_repo_pr_ref(value: object) -> tuple[str, int] | None:
    """Parse ``"owner/repo#N"`` into ``(owner/repo, N)``, or None.

    N is a positive number with no leading zero; a non-str, a bare number, a
    ``#N`` shorthand and a ``.``/``..`` owner or repo segment are all rejected.
    """
    if not isinstance(value, str):
        return None
    match = _REPO_PR_REF_RE.fullmatch(value)
    if match is None or not is_repo_slug(match.group(1)):
        return None
    return match.group(1), int(match.group(2))
