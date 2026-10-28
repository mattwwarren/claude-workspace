"""Tests for ``cw.queue_peek.parse`` -- transcript and sentinel parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cw import queue_peek
from tests._opencode_helpers import (
    earlier_stage_then_final_log,
    framed,
    text_event,
    write_opencode_log,
)
from tests._queue_peek_helpers import (
    make_transcript,
    sentinel_payload,
    sentinel_text,
)


class TestParseTranscript:
    def test_empty_file_returns_all_nones(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] is None
        assert result["last_asst_ts"] is None
        assert result["last_sentinel_status"] is None
        assert result["last_sentinel_stage"] is None
        assert result["last_pr_number"] is None

    def test_extracts_user_and_assistant_timestamps(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": []},
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] == "2026-06-01T10:00:00Z"
        assert result["last_asst_ts"] == "2026-06-01T10:05:00Z"
        assert result["last_sentinel_status"] is None

    def test_first_user_ts_set_only_once(self, tmp_path: Path) -> None:
        """Multiple user messages: first_user_ts captures the earliest."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "first"},
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:10:00Z",
                    "message": {"content": "second"},
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] == "2026-06-01T10:00:00Z"

    def test_handles_bad_json_lines_gracefully(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        user_line = json.dumps(
            {"type": "user", "timestamp": "2026-06-01T10:00:00Z", "message": {}}
        )
        asst_line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-06-01T10:01:00Z",
                "message": {"content": []},
            }
        )
        p.write_text(f"{user_line}\nnot valid json\n{asst_line}\n")
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] == "2026-06-01T10:00:00Z"
        assert result["last_asst_ts"] == "2026-06-01T10:01:00Z"

    def test_nonexistent_file_returns_all_nones(self, tmp_path: Path) -> None:
        """FileNotFoundError (OSError subclass) is caught and returns empty-ish dict."""
        result = queue_peek.parse_transcript(tmp_path / "nope.jsonl")
        assert result["first_user_ts"] is None
        assert result["last_sentinel_status"] is None

    def test_extracts_sentinel_via_auto_dev_result(self, tmp_path: Path) -> None:
        """Valid sentinel in assistant message updates last_sentinel fields."""
        text = sentinel_text(sentinel_payload())
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": [{"type": "text", "text": text}]},
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] == "review_pending_approval"
        assert result["last_sentinel_stage"] == "stage3_review"
        assert result["last_pr_number"] is None

    def test_sentinel_with_pr_number(self, tmp_path: Path) -> None:
        """Sentinel with pr.number populates last_pr_number."""
        pr = {
            "number": 77,
            "url": "https://example.com/pr/77",
            "auto_merge": False,
            "base": "main",
        }
        text = sentinel_text(
            sentinel_payload(status="shipped", stage="stage5_post_create", pr=pr)
        )
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_pr_number"] == 77
        assert result["last_sentinel_status"] == "shipped"

    @staticmethod
    def _tool_result_record(content: object) -> dict[str, Any]:
        return {
            "type": "user",
            "timestamp": "2026-06-01T10:03:00Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "content": content}
                ]
            },
        }

    def test_extracts_pr_number_from_tool_result_when_no_sentinel(
        self, tmp_path: Path
    ) -> None:
        """A session wedged before its finalize sentinel still reports the PR
        `gh pr create` printed into a tool_result (#2251)."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                self._tool_result_record(
                    "Creating pull request\nhttps://github.com/org/repo/pull/456\n"
                )
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_pr_number"] == 456
        assert result["last_sentinel_status"] is None

    def test_sentinel_pr_number_wins_over_tool_result_pr_url(
        self, tmp_path: Path
    ) -> None:
        """A sentinel's pr.number is authoritative over the tool-output fallback
        (#2251)."""
        pr = {
            "number": 77,
            "url": "https://github.com/org/repo/pull/77",
            "auto_merge": False,
            "base": "main",
        }
        text = sentinel_text(
            sentinel_payload(status="shipped", stage="stage5_post_create", pr=pr)
        )
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": [{"type": "text", "text": text}]},
                },
                self._tool_result_record("https://github.com/org/repo/pull/999"),
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_pr_number"] == 77

    def test_last_tool_result_pr_url_wins_when_multiple(self, tmp_path: Path) -> None:
        """Last match in file order wins, matching sentinel semantics (#2251)."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                self._tool_result_record("https://github.com/org/repo/pull/100"),
                self._tool_result_record("https://github.com/org/repo/pull/200"),
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_pr_number"] == 200

    def test_tool_result_content_as_list_of_text_blocks_scanned(
        self, tmp_path: Path
    ) -> None:
        """The list-of-text-sub-blocks tool_result shape is scanned (#2251)."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                self._tool_result_record(
                    [{"type": "text", "text": "https://github.com/org/repo/pull/789"}]
                )
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_pr_number"] == 789

    def test_assistant_prose_pr_url_not_matched_as_fallback(
        self, tmp_path: Path
    ) -> None:
        """Only tool output counts as evidence; a PR URL in assistant prose is
        ignored (#2251)."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "See https://github.com/org/repo/pull/321",
                            }
                        ]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_pr_number"] is None

    def test_no_pr_url_anywhere_returns_none(self, tmp_path: Path) -> None:
        """Ordinary transcript, including non-dict message/content shapes → no
        false-positive PR number (#2251)."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "start"},
                },
                {"type": "user", "timestamp": "2026-06-01T10:01:00Z", "message": "x"},
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:02:00Z",
                    "message": {"content": ["not-a-dict", {"type": "text"}]},
                },
                self._tool_result_record("12 passed in 3.1s"),
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_pr_number"] is None

    def test_skips_documented_example_sentinel(self, tmp_path: Path) -> None:
        """Example sentinel (PROJ-1234, pr#42) is skipped; status stays None."""
        pr = {
            "number": 42,
            "url": "https://example.com/pr/42",
            "auto_merge": False,
            "base": "main",
        }
        example_payload = sentinel_payload(
            ticket_id="PROJ-1234",
            status="shipped",
            stage="stage5_post_create",
            pr=pr,
        )
        example_payload["branch"] = "dev/proj-1234-fix"
        example_payload["plan_source"] = "none"
        text = sentinel_text(example_payload)
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None

    def test_usage_limit_message_sets_flag_true(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "You've hit your session limit · resets 3:45pm",
                            }
                        ]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["usage_limit_detected"] is True

    def test_non_hit_usage_limit_message_sets_flag_true(self, tmp_path: Path) -> None:
        """#2220: a usage-limit banner worded without "hit" is detected."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Usage limit reached · limit resets 3:45pm",
                            }
                        ]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["usage_limit_detected"] is True

    def test_quoted_usage_limit_phrase_does_not_set_flag(self, tmp_path: Path) -> None:
        """#2220: agent prose quoting the banner must not trip the flag."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": 'The message "Usage limit reached" is detected',
                            }
                        ]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["usage_limit_detected"] is False

    def test_no_usage_limit_message_flag_false(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": []},
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["usage_limit_detected"] is False

    def test_usage_limit_detected_outside_sentinel_frame(self, tmp_path: Path) -> None:
        """A plain assistant text block with the limit phrasing and no
        <<<AUTO_DEV_RESULT frame around it is still detected — proves
        detection isn't gated behind extract_block."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Hit your weekly limit · resets Mon 12:00am",
                            }
                        ]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["usage_limit_detected"] is True

    def test_usage_limit_flag_present_alongside_existing_keys(
        self, tmp_path: Path
    ) -> None:
        """Regression: existing returned-dict keys are unchanged; the new key
        is additive only."""
        text = sentinel_text(sentinel_payload())
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": [{"type": "text", "text": text}]},
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] == "2026-06-01T10:00:00Z"
        assert result["last_asst_ts"] == "2026-06-01T10:05:00Z"
        assert result["last_sentinel_status"] == "review_pending_approval"
        assert result["last_sentinel_stage"] == "stage3_review"
        assert result["last_pr_number"] is None
        assert result["usage_limit_detected"] is False

    def test_away_summary_does_not_masquerade_as_last_assistant_activity(
        self, tmp_path: Path
    ) -> None:
        """A trailing ``away_summary`` system record (#2028) must not push
        last_asst_ts forward — only real ``type == "assistant"`` entries
        count, whether this transcript belongs to a parent or a subagent."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": []},
                },
                {
                    "type": "system",
                    "subtype": "away_summary",
                    "content": "session was idle",
                    "timestamp": "2026-06-01T10:20:00Z",
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_asst_ts"] == "2026-06-01T10:00:00Z"


# ---------------------------------------------------------------------------
# parse_transcript — defensive branches
# ---------------------------------------------------------------------------


class TestParseTranscriptDefensiveBranches:
    def test_non_list_content_is_skipped(self, tmp_path: Path) -> None:
        """assistant message with content as a string (not list) is not parsed."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "plain string"},
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None

    def test_non_text_block_type_is_skipped(self, tmp_path: Path) -> None:
        """Block with type != 'text' is skipped."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "content": [{"type": "tool_use", "id": "tu1", "input": {}}]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None

    def test_text_without_sentinel_block_is_skipped(self, tmp_path: Path) -> None:
        """Text block that doesn't contain a sentinel marker is skipped cleanly."""
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "content": [{"type": "text", "text": "just some commentary"}]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None

    def test_non_auto_dev_result_parse_is_skipped(self, tmp_path: Path) -> None:
        """Text with a sentinel marker that parses to a non-AutoDevResult is skipped."""
        # BlockedResult also has <<<...>>> markers but is not AutoDevResult
        blocked = (
            "<<<BLOCKED_RESULT\n"
            '{"schema_version": 1, "reason": "test"}\n'
            "BLOCKED_RESULT>>>"
        )
        p = tmp_path / "t.jsonl"
        make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": [{"type": "text", "text": blocked}]},
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None


def test_parse_opencode_transcript_sentinel_found(tmp_path: Path) -> None:
    """Sentinel in opencode text events → parsed status/stage returned."""
    from cw.opencode_runner import make_blocked

    blocked = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="test")
    sentinel_json = blocked.model_dump_json()
    sentinel_text = f"<<<AUTO_DEV_RESULT\n{sentinel_json}\nAUTO_DEV_RESULT>>>"
    log_path = write_opencode_log(
        tmp_path,
        [
            {"type": "step_start", "part": {"type": "step-start"}},
            {"type": "text", "part": {"text": sentinel_text}},
            {"type": "step_finish", "part": {"reason": "stop"}},
        ],
    )

    result = queue_peek.parse_opencode_transcript(log_path, "T-1")
    assert result["last_sentinel_status"] == "blocked"
    assert result["last_sentinel_stage"] == "stage2_impl"
    assert result["first_user_ts"] is None
    assert result["last_asst_ts"] is not None
    assert result["usage_limit_detected"] is False


def test_parse_opencode_transcript_takes_last_sentinel_over_earlier_stage(
    tmp_path: Path,
) -> None:
    """#2490: peek agrees with harvest -- the final sentinel wins over an earlier one.

    Fixture provenance (composed arrangement, no capture): see
    ``tests/_opencode_helpers.py``.
    """
    from cw.opencode_runner import make_blocked

    earlier = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="impl_failed")
    final = make_blocked(
        ticket_id="T-1",
        worktree=tmp_path,
        reason="merge_conflict_post_push",
        stage_reached="stage4b_pr_create",
    )
    log_path = write_opencode_log(
        tmp_path, earlier_stage_then_final_log(earlier, final)
    )

    result = queue_peek.parse_opencode_transcript(log_path, "T-1")

    assert result["last_sentinel_status"] == "blocked"
    assert result["last_sentinel_stage"] == "stage4b_pr_create"


def test_parse_opencode_transcript_ignores_a_foreign_ticket_quoted_last(
    tmp_path: Path,
) -> None:
    """#2490: peek reports THIS ticket's last sentinel, not a quoted sibling's."""
    from cw.opencode_runner import make_blocked

    real = make_blocked(
        ticket_id="T-1",
        worktree=tmp_path,
        reason="prior_pipeline_pr_open",
        stage_reached="stage4a_merge_gate",
    )
    foreign = make_blocked(
        ticket_id="T-2",
        worktree=tmp_path,
        reason="merge_conflict_post_push",
        stage_reached="stage4b_pr_create",
    )
    log_path = write_opencode_log(
        tmp_path, [text_event(framed(real)), text_event(framed(foreign))]
    )

    result = queue_peek.parse_opencode_transcript(log_path, "T-1")

    assert result["last_sentinel_stage"] == "stage4a_merge_gate"


def test_parse_opencode_transcript_no_sentinel(tmp_path: Path) -> None:
    """No sentinel in text events → None status."""
    log_path = write_opencode_log(
        tmp_path,
        [
            {"type": "text", "part": {"text": "working on it"}},
            {"type": "step_finish", "part": {"reason": "stop"}},
        ],
    )

    result = queue_peek.parse_opencode_transcript(log_path, "T-1")
    assert result["last_sentinel_status"] is None
    assert result["last_sentinel_stage"] is None
    assert result["last_asst_ts"] is not None


def test_parse_opencode_transcript_missing_file(tmp_path: Path) -> None:
    """Missing log file → all fields None/False."""
    result = queue_peek.parse_opencode_transcript(tmp_path / "nonexistent.log", "T-1")
    assert result["last_sentinel_status"] is None
    assert result["last_asst_ts"] is None
    assert result["usage_limit_detected"] is False


def test_parse_opencode_transcript_stat_oserror(tmp_path: Path) -> None:
    """stat() OSError → last_asst_ts is None, sentinel still parsed."""

    log_path = write_opencode_log(
        tmp_path, [{"type": "text", "part": {"text": "no sentinel"}}]
    )
    with patch.object(Path, "stat", side_effect=OSError("denied")):
        result = queue_peek.parse_opencode_transcript(log_path, "T-1")
    assert result["last_sentinel_status"] is None
    assert result["last_asst_ts"] is None
