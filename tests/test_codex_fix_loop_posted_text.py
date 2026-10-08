"""Tests for cw.codex_fix_loop.posted_text — the one redact-and-cap helper (#2633)."""

from __future__ import annotations

from pathlib import Path

import pytest

import cw.codex_fix_loop
from cw.codex_fix_loop.posted_text import (
    ADDED_LINE_MAX_CHARS,
    TRUNCATION_MARKER,
    WITHHELD,
    describe_added_line,
    redact_and_cap,
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
    @pytest.mark.parametrize("module", ["hook_failure"])
    def test_guards_import_the_helper(self, module: str) -> None:
        package = Path(cw.codex_fix_loop.__file__).parent
        source = (package / f"{module}.py").read_text(encoding="utf-8")

        assert "posted_text" in source
        assert "cw._text" not in source
