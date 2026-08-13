"""Tests for cw.codex_review._const — reason vocabulary totality (#1236)."""

from __future__ import annotations

from typing import get_args

from cw.codex_review import _CATEGORY_TO_REASON, _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS
from cw.executor_diagnostics import ExecutorFailureCategory


def test_category_to_reason_mapping_is_total() -> None:
    """Every ExecutorFailureCategory member is a key in _CATEGORY_TO_REASON —
    guards the total-dict design decision against a future silent KeyError
    (item 5, #1330)."""
    for category in get_args(ExecutorFailureCategory):
        assert category in _CATEGORY_TO_REASON


def test_codex_review_blocked_next_actions_is_user_directed() -> None:
    """_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS must satisfy schema.py's
    USER_DIRECTED_PREFIXES — it is the only escape hatch that lets a
    ``blocked``/terminal-reject AutoDevResult carry non-empty next_actions
    (#1835). A constant that drifts off this prefix would crash
    AutoDevResult construction at every codex_review blocked call site."""
    assert _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS[0].startswith(
        ("user_resolve_", "user_decide_", "user_verify_")
    )
