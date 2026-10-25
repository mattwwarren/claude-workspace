"""Tests for cw._usage_limit_patterns - the usage-limit wording table (#2220).

The wordings come from the installed ``claude`` binary and its docs (not from a
captured spawn message), so the fixtures below are derived from those strings.
"""

from __future__ import annotations

import re
import time

import pytest

from cw._usage_limit_patterns import USAGE_LIMIT_PATTERNS
from cw.exceptions import USAGE_LIMIT_RE

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
# Generous wall-clock guard for the adversarial inputs (measured ~0.03s).
_REDOS_BUDGET_SECONDS = 2.0
_REPEAT_LARGE = 10_000
_REPEAT_MEDIUM = 5_000

# One realistic full message per table entry. Adding an entry to the table
# without a fixture here fails test_every_entry_has_a_fixture.
_FIXTURE_BY_ENTRY: dict[str, tuple[str, ...]] = {
    "hit_your": ("You've hit your session limit · resets 3:45pm",),
    "usage_limit_reached": (
        "Usage limit reached",
        "Usage limit reached again",
    ),
    "usage_limit_semicolon_resets": (
        "usage limit; resets 3:45pm, too far out to wait for",
    ),
    "until_your_limit_resets_at": (
        "Continuing now at lower priority until your limit resets at 3:45pm. "
        "Your weekly limit still applies",
    ),
    "limit_resets_time": (
        "Now using usage credits for Opus · Your Opus limit resets 3:45pm",
        "Usage limit reached · limit resets Mon 12:00am",
    ),
}


class TestUsageLimitPatternTable:
    def test_names_unique_and_snake_case(self) -> None:
        names = [entry.name for entry in USAGE_LIMIT_PATTERNS]

        assert len(names) == len(set(names))
        assert all(_SNAKE_CASE.match(name) for name in names)

    def test_every_entry_has_nonempty_source_provenance(self) -> None:
        for entry in USAGE_LIMIT_PATTERNS:
            assert entry.source.strip(), entry.name
            assert "2.1." in entry.source or "errors.md" in entry.source, entry.name

    def test_every_entry_compiles_standalone(self) -> None:
        for entry in USAGE_LIMIT_PATTERNS:
            re.compile(entry.pattern, re.IGNORECASE)

    def test_every_entry_has_a_fixture(self) -> None:
        assert {e.name for e in USAGE_LIMIT_PATTERNS} == set(_FIXTURE_BY_ENTRY)

    def test_each_entry_matches_its_own_fixture_standalone(self) -> None:
        for entry in USAGE_LIMIT_PATTERNS:
            compiled = re.compile(entry.pattern, re.IGNORECASE)
            for fixture in _FIXTURE_BY_ENTRY[entry.name]:
                assert compiled.search(fixture), (entry.name, fixture)

    @pytest.mark.parametrize(
        "fixture",
        [text for texts in _FIXTURE_BY_ENTRY.values() for text in texts],
    )
    def test_combined_regex_matches_every_fixture(self, fixture: str) -> None:
        assert USAGE_LIMIT_RE.search(fixture)


class TestUsageLimitNegatives:
    @pytest.mark.parametrize(
        "text",
        [
            "rate limit exceeded",
            "the line limit is 80",
            "limit resets per request",
            "memory limit reached",
            "file size limit reached",
            "connection limit exceeded",
            "hit the wall",
            "the rate limit resets at 9am daily",
            "Rate limit resets at 3pm",
            "rate-limit resets 3pm",
            "- limit resets 3pm",
            "| limit resets 3pm",
            "token limit resets every minute",
            "the limit resets 3pm daily",
            "add a test for usage limit; resets handling",
            "wait until your limit resets",
            'I will quote the message "Usage limit reached" in the test',
            "see `Usage limit reached`",
            "the usage limit reached 80% earlier",
        ],
    )
    def test_prose_and_unrelated_limits_do_not_match(self, text: str) -> None:
        assert USAGE_LIMIT_RE.search(text) is None


class TestReDoSBounds:
    @pytest.mark.parametrize(
        "text",
        [
            "limit " * _REPEAT_LARGE,
            "usage limit; " * _REPEAT_LARGE,
            "until your limit resets at " * _REPEAT_MEDIUM,
            "· limit resets " * _REPEAT_MEDIUM,
        ],
        ids=["limit", "usage-limit-semicolon", "until-your-limit", "middot-limit"],
    )
    def test_adversarial_input_finishes_quickly(self, text: str) -> None:
        start = time.perf_counter()
        list(USAGE_LIMIT_RE.finditer(text))

        assert time.perf_counter() - start < _REDOS_BUDGET_SECONDS
