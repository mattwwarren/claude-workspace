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
