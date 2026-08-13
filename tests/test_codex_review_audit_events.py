"""Tests for cw.codex_review._audit_events — the ``codex exec --json`` JSONL
audit-event parser (#1710).

Every fixture under ``tests/fixtures/codex_audit_events/`` is a real, redacted
capture from ``codex-cli 0.147.0`` (placeholder thread ids / error text; shape
untouched) — codex is a system we do not own, so the parser's contract is built
from observed payloads, never invented ones.

``_parse_codex_audit_events`` deliberately never logs: it has no ``role`` /
``session_id`` in scope to attribute a warning to. The malformed/incomplete
terminal-event warning is emitted one layer up by ``_run_codex_role``
(see ``tests/test_codex_review_roles.py``), so nothing here asserts on caplog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.codex_review._audit_events import (
    _extract_terminal_error_message,
    _parse_codex_audit_events,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex_audit_events"


def _fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


class TestParseCodexAuditEventsCleanNoTools:
    def test_thread_terminal_and_usage_fields(self) -> None:
        metrics = _parse_codex_audit_events(
            _fixture("clean_no_tools.jsonl"), duration_seconds=1.5
        )
        assert metrics["thread_id"] == "<THREAD_ID>"
        assert metrics["terminal_event"] == "turn.completed"
        assert metrics["input_tokens"] == 13239
        assert metrics["cached_input_tokens"] == 9984
        assert metrics["output_tokens"] == 5
        # Wire key is ``reasoning_output_tokens``; the field is ``reasoning_tokens``.
        assert metrics["reasoning_tokens"] == 0

    def test_tool_counts_and_command_evidence(self) -> None:
        metrics = _parse_codex_audit_events(
            _fixture("clean_no_tools.jsonl"), duration_seconds=1.5
        )
        assert metrics["tool_call_counts"] == {"agent_message": 1}
        assert metrics["had_command_evidence"] is False
        assert metrics["unexpected_tool_attempts"] == []

    def test_duration_passed_through_unchanged(self) -> None:
        metrics = _parse_codex_audit_events(
            _fixture("clean_no_tools.jsonl"), duration_seconds=42.25
        )
        assert metrics["duration_seconds"] == pytest.approx(42.25)

    def test_effective_model_is_always_none(self) -> None:
        # No codex-cli 0.147.0 event observed carries a model field anywhere,
        # so this stays None (adopted assumption 1, #1710).
        metrics = _parse_codex_audit_events(
            _fixture("clean_no_tools.jsonl"), duration_seconds=1.0
        )
        assert metrics["effective_model"] is None


class TestParseCodexAuditEventsWithCommand:
    def test_completed_only_counting(self) -> None:
        # item_1 appears as both item.started and item.completed; only the
        # completed event increments the count.
        metrics = _parse_codex_audit_events(
            _fixture("clean_with_command.jsonl"), duration_seconds=3.0
        )
        assert metrics["tool_call_counts"]["command_execution"] == 1
        assert metrics["tool_call_counts"]["agent_message"] == 2

    def test_command_evidence_flagged(self) -> None:
        metrics = _parse_codex_audit_events(
            _fixture("clean_with_command.jsonl"), duration_seconds=3.0
        )
        assert metrics["had_command_evidence"] is True
        assert metrics["unexpected_tool_attempts"] == []
        assert metrics["terminal_event"] == "turn.completed"


class TestParseCodexAuditEventsFailedTurn:
    def test_terminal_event_is_turn_failed(self) -> None:
        metrics = _parse_codex_audit_events(
            _fixture("failed_turn.jsonl"), duration_seconds=2.0
        )
        assert metrics["terminal_event"] == "turn.failed"

    def test_thread_id_still_populated_tokens_absent(self) -> None:
        metrics = _parse_codex_audit_events(
            _fixture("failed_turn.jsonl"), duration_seconds=2.0
        )
        assert metrics["thread_id"] == "<THREAD_ID>"
        assert metrics["input_tokens"] is None
        assert metrics["output_tokens"] is None


class TestParseCodexAuditEventsTruncated:
    def test_terminal_event_is_not_a_turn_terminator(self) -> None:
        metrics = _parse_codex_audit_events(
            _fixture("truncated_mid_command.jsonl"), duration_seconds=9.0
        )
        assert metrics["terminal_event"] not in {"turn.completed", "turn.failed"}
        assert metrics["terminal_event"] == "item.completed"

    def test_partial_stream_still_yields_what_was_seen(self) -> None:
        metrics = _parse_codex_audit_events(
            _fixture("truncated_mid_command.jsonl"), duration_seconds=9.0
        )
        assert metrics["thread_id"] == "<THREAD_ID>"
        assert metrics["tool_call_counts"] == {"agent_message": 1}


class TestParseCodexAuditEventsDegradesGracefully:
    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            "   \n\n  ",
            "this is not json at all\nneither is this",
            "[]",
            "null",
            '"just a string"',
            "123",
        ],
    )
    def test_non_jsonl_stdout_yields_all_defaults(self, stdout: str) -> None:
        # This tolerance is what makes it safe to feed a flag-rejection
        # retry's (non-``--json``) stdout straight into the parser.
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["thread_id"] is None
        assert metrics["terminal_event"] is None
        assert metrics["input_tokens"] is None
        assert metrics["cached_input_tokens"] is None
        assert metrics["output_tokens"] is None
        assert metrics["reasoning_tokens"] is None
        assert metrics["tool_call_counts"] == {}
        assert metrics["had_command_evidence"] is False
        assert metrics["unexpected_tool_attempts"] == []
        assert metrics["duration_seconds"] == pytest.approx(1.0)

    def test_garbage_lines_interleaved_with_valid_ones_are_skipped(self) -> None:
        stdout = (
            '{"type":"thread.started","thread_id":"T"}\n'
            "<<< not json >>>\n"
            '{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":2}}\n'
        )
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["thread_id"] == "T"
        assert metrics["terminal_event"] == "turn.completed"
        assert metrics["input_tokens"] == 7
        assert metrics["output_tokens"] == 2

    def test_non_int_usage_values_degrade_to_none(self) -> None:
        stdout = (
            '{"type":"turn.completed","usage":'
            '{"input_tokens":"lots","output_tokens":true,"reasoning_output_tokens":3}}\n'
        )
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["input_tokens"] is None
        assert metrics["output_tokens"] is None
        assert metrics["reasoning_tokens"] == 3

    def test_event_with_a_non_string_type_is_skipped_entirely(self) -> None:
        # A dict line whose "type" is absent or not a string carries no
        # recognizable event, so it must not become terminal_event either.
        stdout = (
            '{"type":"thread.started","thread_id":"T"}\n'
            '{"type":42,"thread_id":"NOPE"}\n'
            '{"no_type_at_all":true}\n'
        )
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["thread_id"] == "T"
        assert metrics["terminal_event"] == "thread.started"

    def test_usage_block_that_is_not_a_dict_is_ignored(self) -> None:
        stdout = '{"type":"turn.completed","usage":"nope"}\n'
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["terminal_event"] == "turn.completed"
        assert metrics["input_tokens"] is None


class TestParseCodexAuditEventsUnexpectedToolAttempts:
    def test_unknown_item_type_is_flagged(self) -> None:
        # Synthetic (our own allowlist, not codex's schema): anything outside
        # _EXPECTED_REVIEWER_ITEM_TYPES is recorded observationally.
        stdout = (
            '{"type":"item.completed","item":'
            '{"id":"item_0","type":"mcp_tool_call","server":"x"}}\n'
        )
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["unexpected_tool_attempts"] == ["mcp_tool_call"]

    def test_started_only_unknown_type_is_also_flagged(self) -> None:
        stdout = (
            '{"type":"item.started","item":'
            '{"id":"item_0","type":"web_search","query":"x"}}\n'
        )
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["unexpected_tool_attempts"] == ["web_search"]
        # started-only: never counted as a completed tool call.
        assert metrics["tool_call_counts"] == {}

    def test_unexpected_types_are_deduped_in_order(self) -> None:
        stdout = (
            '{"type":"item.started","item":{"id":"i0","type":"mcp_tool_call"}}\n'
            '{"type":"item.completed","item":{"id":"i0","type":"mcp_tool_call"}}\n'
            '{"type":"item.completed","item":{"id":"i1","type":"web_search"}}\n'
        )
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["unexpected_tool_attempts"] == ["mcp_tool_call", "web_search"]

    def test_expected_types_never_flagged(self) -> None:
        stdout = (
            '{"type":"item.completed","item":{"id":"i0","type":"reasoning"}}\n'
            '{"type":"item.completed","item":{"id":"i1","type":"error"}}\n'
        )
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["unexpected_tool_attempts"] == []
        assert metrics["tool_call_counts"] == {"reasoning": 1, "error": 1}

    def test_item_without_a_dict_payload_is_ignored(self) -> None:
        stdout = (
            '{"type":"item.completed","item":"nope"}\n'
            '{"type":"item.completed","item":{"id":"i0"}}\n'
        )
        metrics = _parse_codex_audit_events(stdout, duration_seconds=1.0)
        assert metrics["tool_call_counts"] == {}
        assert metrics["unexpected_tool_attempts"] == []


class TestExtractTerminalErrorMessage:
    """#1836: the terminal ``turn.failed`` event's ``error.message``.

    Deliberately a free function rather than a ``ReviewerRunMetrics`` field —
    the value drives a failure *classification* (retry-eligibility), and that
    metrics bag's documented invariant (R3, #1710) is that nothing in it is
    read by health, blocking, or gate logic.
    """

    def test_capacity_message_extracted(self) -> None:
        assert (
            _extract_terminal_error_message(_fixture("capacity_turn_failed.jsonl"))
            == "Selected model is at capacity. Please try a different model."
        )

    def test_redacted_failed_turn_message_extracted(self) -> None:
        assert (
            _extract_terminal_error_message(_fixture("failed_turn.jsonl"))
            == "<redacted upstream error>"
        )

    def test_no_turn_failed_returns_none(self) -> None:
        assert _extract_terminal_error_message(_fixture("clean_no_tools.jsonl")) is None

    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            "   \n\n  ",
            "this is not json at all\nneither is this",
            "[]",
            "null",
            '"just a string"',
            "123",
        ],
    )
    def test_non_jsonl_stdout_returns_none(self, stdout: str) -> None:
        assert _extract_terminal_error_message(stdout) is None

    def test_error_field_not_a_dict_returns_none(self) -> None:
        stdout = '{"type":"turn.failed","error":"boom"}\n'
        assert _extract_terminal_error_message(stdout) is None

    def test_message_field_not_a_string_returns_none(self) -> None:
        stdout = '{"type":"turn.failed","error":{"message":42}}\n'
        assert _extract_terminal_error_message(stdout) is None

    def test_last_turn_failed_wins(self) -> None:
        stdout = (
            '{"type":"turn.failed","error":{"message":"first"}}\n'
            '{"type":"turn.failed","error":{"message":"second"}}\n'
        )
        assert _extract_terminal_error_message(stdout) == "second"
