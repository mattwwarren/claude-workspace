"""Tests for cw.stage_verification.posted_text — the one redact-and-cap helper
(#2633)."""

from __future__ import annotations

from pathlib import Path

import pytest

import cw.codex_fix_loop
from cw.stage_verification.posted_text import (
    ADDED_LINE_MAX_CHARS,
    TRUNCATION_MARKER,
    WITHHELD,
    describe_added_line,
    redact_and_cap,
    redact_and_cap_bytes,
    withhold_secret_lines,
)

_TOKEN = "ghp_" + "a" * 36


class TestRedactAndCap:
    def test_redacts_before_capping(self) -> None:
        text = "x" * (ADDED_LINE_MAX_CHARS - 10) + _TOKEN

        out = redact_and_cap(text)

        assert "ghp_" not in out
        assert "<redacted>" in out

    def test_per_line_cap_defaults_to_120_chars(self) -> None:
        assert redact_and_cap("y" * 500) == "y" * 120 + TRUNCATION_MARKER

    def test_line_count_and_total_caps_are_honoured(self) -> None:
        text = "\n".join(f"line{i}" for i in range(10))

        by_lines = redact_and_cap(text, max_lines=3)
        by_total = redact_and_cap(text, max_total_chars=12)

        assert by_lines == "line0\nline1\nline2" + TRUNCATION_MARKER
        assert by_total == "line0\nline1\n" + TRUNCATION_MARKER
        assert by_lines.count(TRUNCATION_MARKER) == 1

    def test_empty_and_short_text_pass_through_unchanged(self) -> None:
        assert redact_and_cap("") == ""
        assert redact_and_cap("short\ntext") == "short\ntext"


class TestRedactAndCapBytes:
    def test_under_cap_text_passes_through(self) -> None:
        assert redact_and_cap_bytes("short\ntext", cap_bytes=100) == "short\ntext"

    def test_over_cap_multibyte_text_fits_and_ends_with_marker(self) -> None:
        out = redact_and_cap_bytes("é" * 500, cap_bytes=200)

        assert len(out.encode("utf-8")) <= 200
        assert out.endswith(TRUNCATION_MARKER)
        assert out.count(TRUNCATION_MARKER) == 1

    def test_byte_overflow_below_char_cap_is_cut(self) -> None:
        # 120 two-byte chars: under a 150-char cap, over a 150-byte cap.
        out = redact_and_cap_bytes("é" * 120, cap_bytes=150)

        assert len(out.encode("utf-8")) <= 150
        assert out.endswith(TRUNCATION_MARKER)

    def test_redacts_before_capping(self) -> None:
        out = redact_and_cap_bytes("x" * 50 + _TOKEN, cap_bytes=1000)

        assert "ghp_" not in out
        assert "<redacted>" in out

    def test_long_line_is_not_cut_at_the_posted_text_default(self) -> None:
        text = "y" * (ADDED_LINE_MAX_CHARS * 5)

        assert redact_and_cap_bytes(text, cap_bytes=10_000) == text

    def test_cut_never_splits_a_utf8_sequence(self) -> None:
        for cap in range(40, 60):
            out = redact_and_cap_bytes("é" * 100, cap_bytes=cap)

            out.encode("utf-8").decode("utf-8")
            assert len(out.encode("utf-8")) <= cap


class TestDescribeAddedLine:
    def test_plain_code_line_is_redacted_and_capped(self) -> None:
        assert describe_added_line("return compute(x)") == "return compute(x)"
        assert len(describe_added_line("z" * 300)) == 120 + len(TRUNCATION_MARKER)

    @pytest.mark.parametrize(
        "line",
        [
            'password = "hunter2"',
            "API_KEY = 'abc'",
            "self.secret: str = value",
            "aws_secret_access_key=x",
            'db_passwd = "p"',
            'token = "t"',
            '"password": "x"',
            "{'api_key': 'x'}",
        ],
    )
    def test_secret_named_assignment_is_withheld(self, line: str) -> None:
        assert describe_added_line(line) == WITHHELD

    def test_aws_key_id_is_withheld(self) -> None:
        assert describe_added_line("key = lookup('AKIAIOSFODNN7EXAMPLE')") == WITHHELD

    def test_non_secret_identifier_named_like_a_keyword_is_not_withheld(
        self,
    ) -> None:
        assert describe_added_line("monkey_patch = 1") == "monkey_patch = 1"

    def test_withhold_secret_lines_is_per_line(self) -> None:
        out = withhold_secret_lines("ok line\npassword=hunter2\nfine")

        assert out == f"ok line\n{WITHHELD}\nfine"


class TestRedactAndCapIsTheOnlyPostedPath:
    @pytest.mark.parametrize(
        ("subpackage", "module"),
        [
            ("codex_fix_loop", "constraints"),
            ("codex_fix_loop", "duplicate_reader"),
            ("stage_verification", "growth"),
            ("codex_fix_loop", "hook_failure"),
        ],
    )
    def test_guards_import_the_helper(self, subpackage: str, module: str) -> None:
        source = (Path(cw.__file__).parent / subpackage / f"{module}.py").read_text(
            encoding="utf-8"
        )

        assert "posted_text" in source
        assert "cw._text" not in source
