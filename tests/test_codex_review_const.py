"""Tests for cw.codex_review._const — reason vocabulary totality (#1236)."""

from __future__ import annotations

from typing import get_args

import pytest

from cw.codex_review import (
    _CATEGORY_TO_REASON,
    _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
    _TRANSIENT_FAILURE_REASONS,
    BACKEND_DOWN_REASONS,
    CODEX_ERROR,
    CODEX_FIX_CONSTRAINT_VIOLATION,
    CODEX_FIX_DIRTY_START,
    CODEX_FIX_GATE_FAILED,
    CODEX_FIX_GROWTH_BUDGET,
    CODEX_FIX_HOOK_FAILED,
    CODEX_INPUT_TOO_LARGE,
    CODEX_MAX_PROMPT_CHARS,
    CODEX_MODEL_CAPACITY,
    INPUT_SIZES_PATTERN,
    format_input_sizes,
)
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


def test_codex_model_capacity_is_transient() -> None:
    """#1836: a provider-capacity blip must be retry-eligible, not parked."""
    assert CODEX_MODEL_CAPACITY in _TRANSIENT_FAILURE_REASONS


def test_codex_model_capacity_is_its_own_reason_code() -> None:
    """#1836: distinct from the generic CODEX_ERROR bucket, and never wired
    into _CATEGORY_TO_REASON — the override lives in _run_codex_role, so the
    fine-grained category taxonomy stays untouched."""
    assert CODEX_MODEL_CAPACITY != CODEX_ERROR
    assert CODEX_MODEL_CAPACITY not in _CATEGORY_TO_REASON.values()


_FIX_LOOP_GUARD_REASONS = (
    CODEX_FIX_DIRTY_START,
    CODEX_FIX_HOOK_FAILED,
    CODEX_FIX_GATE_FAILED,
    CODEX_FIX_GROWTH_BUDGET,
    CODEX_FIX_CONSTRAINT_VIOLATION,
)


def test_fix_loop_guard_reasons_are_distinct() -> None:
    """#2633: each new fix-loop guard parks under its own reason code."""
    assert len(set(_FIX_LOOP_GUARD_REASONS)) == len(_FIX_LOOP_GUARD_REASONS)


@pytest.mark.parametrize("reason", _FIX_LOOP_GUARD_REASONS)
def test_fix_loop_guard_reasons_are_operator_parks(reason: str) -> None:
    """#2633: a guard-rejected cycle needs an operator, never a blind retry,
    and is not a codex-invocation failure category."""
    assert reason not in _TRANSIENT_FAILURE_REASONS
    assert reason not in _CATEGORY_TO_REASON.values()


def test_codex_input_too_large_is_a_content_failure_not_an_outage() -> None:
    """#2864: a prompt-size rejection is exit 4 on a healthy backend.

    Not backend-down (so ``cw review confirm`` never reports it as an
    outage), not transient (the same input fails the same way on a retry),
    and not in the category mapping (it reclassifies ``nonzero_exit``).
    """
    assert CODEX_INPUT_TOO_LARGE not in BACKEND_DOWN_REASONS
    assert CODEX_INPUT_TOO_LARGE not in _TRANSIENT_FAILURE_REASONS
    assert CODEX_INPUT_TOO_LARGE not in _CATEGORY_TO_REASON.values()
    assert CODEX_INPUT_TOO_LARGE != CODEX_ERROR


def test_codex_max_prompt_chars_is_the_observed_limit() -> None:
    """#2864: the limit the codex backend reported (``max_chars 1048576``)."""
    assert CODEX_MAX_PROMPT_CHARS == 1_048_576


def test_input_sizes_format_round_trips_through_the_shared_pattern() -> None:
    """#2864: producers format and the CLI parses with one shared definition."""
    text = format_input_sizes(1379349, 1048576)
    assert text == "actual_chars=1379349 max_chars=1048576"
    match = INPUT_SIZES_PATTERN.search(f"role (codex_input_too_large): {text}")
    assert match is not None
    assert match.groups() == ("1379349", "1048576")
