"""Dependency-free value predicates shared across cw's layers.

Imports nothing from ``cw``, so both the persisted-model layer and the
sentinel schema layer can depend on it without a cycle. Lives outside
``cw.models.enums`` because a generic predicate is not an enum concern.
"""

from __future__ import annotations

from typing import TypeGuard


def is_positive_real_int(value: object) -> TypeGuard[int]:
    """Return whether *value* is a positive, non-boolean integer."""
    return type(value) is int and value > 0
