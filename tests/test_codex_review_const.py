"""Tests for cw.codex_review._const — reason vocabulary totality (#1236)."""

from __future__ import annotations

from typing import get_args

from cw.codex_review import _CATEGORY_TO_REASON
from cw.executor_diagnostics import ExecutorFailureCategory


def test_category_to_reason_mapping_is_total() -> None:
    """Every ExecutorFailureCategory member is a key in _CATEGORY_TO_REASON —
    guards the total-dict design decision against a future silent KeyError
    (item 5, #1330)."""
    for category in get_args(ExecutorFailureCategory):
        assert category in _CATEGORY_TO_REASON
