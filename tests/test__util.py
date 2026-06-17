"""Tests for cw._util - shared utility helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestIterAssistantTextBlocks:
    """Tests for _iter_assistant_text_blocks (shared transcript walker)."""

    def test_yields_assistant_text_skipping_other_records(self, tmp_path: Path) -> None:
        """Yields assistant text blocks in order, skipping non-assistant and
        malformed records."""
        from cw._util import _iter_assistant_text_blocks

        transcript = tmp_path / "t.jsonl"
        records = [
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "ignored"}],
                    },
                }
            ),
            "{ not valid json",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "first"},
                            {"type": "tool_use", "name": "noise"},
                            {"type": "text", "text": "second"},
                        ],
                    },
                }
            ),
        ]
        transcript.write_text("\n".join(records) + "\n")

        assert list(_iter_assistant_text_blocks(transcript)) == ["first", "second"]

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        from cw._util import _iter_assistant_text_blocks

        assert list(_iter_assistant_text_blocks(tmp_path / "nope.jsonl")) == []

    def test_oserror_on_open_yields_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An I/O error mid-read is swallowed — the walker yields nothing."""
        from cw._util import _iter_assistant_text_blocks

        transcript = tmp_path / "t.jsonl"
        transcript.write_text('{"type": "assistant"}\n')

        def _boom(*_a: object, **_kw: object) -> None:
            msg = "boom"
            raise OSError(msg)

        monkeypatch.setattr("pathlib.Path.open", _boom)
        assert list(_iter_assistant_text_blocks(transcript)) == []


class TestIterSentinelTextBlocks:
    """Tests for _iter_sentinel_text_blocks (assistant text + tool_result stdout).

    A worker may emit the AUTO_DEV_RESULT sentinel via a Bash ``cat`` command,
    landing it in a tool_result block rather than assistant text (#731). The
    sentinel scanner must see both — but NOT user prose or the tool_use command
    echo (which would duplicate the frame / pull in the prompt's schema example).
    """

    def test_yields_assistant_text_and_tool_result_skipping_user_prose(
        self, tmp_path: Path
    ) -> None:
        from cw._util import _iter_sentinel_text_blocks

        transcript = tmp_path / "t.jsonl"
        records = [
            # user prose (e.g. prompt) — must be skipped to avoid false frames
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "prompt prose"}],
                    },
                }
            ),
            # assistant text + tool_use command echo (tool_use must be skipped)
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "narrative"},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "cat <<EOF\nframe\nEOF"},
                            },
                        ],
                    },
                }
            ),
            # tool_result (Bash stdout) carrying the real sentinel — string form
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "content": "stdout-sentinel"}
                        ],
                    },
                }
            ),
            # tool_result list form — text sub-blocks yielded
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "list-stdout"}],
                            }
                        ],
                    },
                }
            ),
        ]
        transcript.write_text("\n".join(records) + "\n")

        assert list(_iter_sentinel_text_blocks(transcript)) == [
            "narrative",
            "stdout-sentinel",
            "list-stdout",
        ]

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        from cw._util import _iter_sentinel_text_blocks

        assert list(_iter_sentinel_text_blocks(tmp_path / "nope.jsonl")) == []

    def test_malformed_and_non_dict_records_skipped(self, tmp_path: Path) -> None:
        """Invalid JSON, non-dict records, non-list content, non-dict blocks are
        all skipped rather than raising."""
        from cw._util import _iter_sentinel_text_blocks

        transcript = tmp_path / "t.jsonl"
        records = [
            "{ not valid json",
            json.dumps([1, 2, 3]),  # record is not a dict
            json.dumps({"type": "assistant", "message": "not-a-dict"}),
            json.dumps({"type": "assistant", "message": {"content": "not-a-list"}}),
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": ["not-a-dict-block"]},
                }
            ),
        ]
        transcript.write_text("\n".join(records) + "\n")

        assert list(_iter_sentinel_text_blocks(transcript)) == []

    def test_oserror_on_open_yields_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw._util import _iter_sentinel_text_blocks

        transcript = tmp_path / "t.jsonl"
        transcript.write_text('{"type": "assistant"}\n')

        def _boom(*_a: object, **_kw: object) -> None:
            msg = "boom"
            raise OSError(msg)

        monkeypatch.setattr("pathlib.Path.open", _boom)
        assert list(_iter_sentinel_text_blocks(transcript)) == []
