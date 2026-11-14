"""Tests for cw.dispatch_state_marks_parse, the stdlib-only clear-mark leaf (#3547)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cw.dispatch_state_marks_parse as leaf
from cw.dispatch_state_marks_parse import (
    CLEAR_MARK_RETENTION,
    CLEAR_MARKS_KEY,
    drop_unseen_cleared,
    unseen_clear_marks,
    usage_limit_clear_marks_from_payload,
)

_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(minutes=5)
_UNTIL = _T0 + timedelta(hours=3)


class TestParse:
    def test_valid_aware_marks_are_parsed(self) -> None:
        payload = {CLEAR_MARKS_KEY: {"a": _T0.isoformat(), "b": _T1.isoformat()}}

        assert usage_limit_clear_marks_from_payload(payload) == {"a": _T0, "b": _T1}

    def test_malformed_naive_and_non_string_values_are_skipped(self) -> None:
        payload = {
            CLEAR_MARKS_KEY: {
                "good": _T0.isoformat(),
                "garbage": "not-a-date",
                "naive": "2026-11-01T12:00:00",
                "number": 12,
                "null": None,
            }
        }

        assert usage_limit_clear_marks_from_payload(payload) == {"good": _T0}

    def test_absent_key_or_non_dict_value_gives_empty(self) -> None:
        assert usage_limit_clear_marks_from_payload({}) == {}
        assert usage_limit_clear_marks_from_payload({CLEAR_MARKS_KEY: "x"}) == {}
        assert usage_limit_clear_marks_from_payload({CLEAR_MARKS_KEY: [1]}) == {}

    def test_no_expiry_filter(self) -> None:
        old = _T0 - CLEAR_MARK_RETENTION - timedelta(days=30)
        payload = {CLEAR_MARKS_KEY: {"a": old.isoformat()}}

        assert usage_limit_clear_marks_from_payload(payload) == {"a": old}

    def test_key_and_retention_constants(self) -> None:
        assert CLEAR_MARKS_KEY == "usage_limit_cleared"
        assert timedelta(days=7) == CLEAR_MARK_RETENTION


class TestUnseen:
    def test_mark_differing_from_seen_is_unseen(self) -> None:
        assert unseen_clear_marks({"a": _T1}, {"a": _T0}) == {"a"}

    def test_mark_equal_to_seen_is_seen(self) -> None:
        assert unseen_clear_marks({"a": _T0}, {"a": _T0}) == set()

    def test_client_with_mark_and_no_seen_entry_is_unseen(self) -> None:
        assert unseen_clear_marks({"a": _T0}, {}) == {"a"}

    def test_client_with_no_mark_is_never_returned(self) -> None:
        assert unseen_clear_marks({}, {"a": _T0}) == set()


class TestDropUnseenCleared:
    def test_drops_only_unseen_mark_clients(self) -> None:
        windows = {"a": _UNTIL, "b": _UNTIL, "c": _UNTIL}
        marks = {"a": _T1, "b": _T0}
        seen = {"b": _T0}

        kept, dropped = drop_unseen_cleared(windows, marks, seen)

        assert kept == {"b": _UNTIL, "c": _UNTIL}
        assert dropped == {"a"}

    def test_unseen_mark_with_no_window_drops_nothing(self) -> None:
        kept, dropped = drop_unseen_cleared({"b": _UNTIL}, {"a": _T0}, {})

        assert kept == {"b": _UNTIL}
        assert dropped == set()

    def test_inputs_are_not_mutated(self) -> None:
        windows = {"a": _UNTIL}
        marks = {"a": _T0}
        seen: dict[str, datetime] = {}

        drop_unseen_cleared(windows, marks, seen)

        assert windows == {"a": _UNTIL}
        assert marks == {"a": _T0}
        assert seen == {}


def test_leaf_imports_nothing_from_cw() -> None:
    """The leaf stays stdlib-only so ``cw.dispatch_state`` can import it."""
    source = Path(leaf.__file__).read_text()
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)

    assert modules
    assert not [m for m in modules if m == "cw" or m.startswith("cw.")]
