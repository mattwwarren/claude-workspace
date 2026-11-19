"""Tests for the shared Claude model-id parser (#3626)."""

from __future__ import annotations

import pytest

from cw.model_family import ModelVersion, parse_model_id
from cw.worker_effort import _default_applies


class TestParseModelIdPositives:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("claude-opus-5-5", ModelVersion("opus", 5, 5)),
            ("claude-opus-5-5-20260101", ModelVersion("opus", 5, 5)),
            ("claude-sonnet-4-6-20251015", ModelVersion("sonnet", 4, 6)),
            ("claude-sonnet-5", ModelVersion("sonnet", 5, None)),
            # A date directly after the major must not be read as a minor.
            ("claude-sonnet-5-20260101", ModelVersion("sonnet", 5, None)),
            ("claude-opus-4-20250514", ModelVersion("opus", 4, None)),
            ("claude-fable-5-2", ModelVersion("fable", 5, 2)),
            ("claude-opus-10", ModelVersion("opus", 10, None)),
            ("  CLAUDE-OPUS-5-5  ", ModelVersion("opus", 5, 5)),
        ],
    )
    def test_parses_family_first_ids(self, model: str, expected: ModelVersion) -> None:
        assert parse_model_id(model) == expected

    def test_dotted_version_tail_is_accepted(self) -> None:
        """Accept-any-tail rule: `claude-opus-5.5` parses as major 5."""
        assert parse_model_id("claude-opus-5.5") == ModelVersion("opus", 5, None)


class TestParseModelIdNone:
    @pytest.mark.parametrize(
        "model",
        [
            None,
            "",
            "opus",
            "sonnet",
            "haiku",
            "claude-3-5-sonnet-20241022",
            "claude-3-7-sonnet",
            # A date directly after the family must not parse as major 20250101.
            "claude-sonnet-20250101",
            "claude-opus-",
            "gpt-5",
            # Start-anchored: provider-prefixed ids are unsupported.
            "us.anthropic.claude-opus-5-5",
        ],
    )
    def test_unparseable_returns_none(self, model: str | None) -> None:
        assert parse_model_id(model) is None


class TestDriftGuard:
    @pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-opus-5-5-20260101"])
    def test_effort_default_ids_are_auto_capable(self, model: str) -> None:
        """Every id that gets the Opus 5.5 effort default must also be auto-capable."""
        from cw.native_daemon import model_supports_auto

        assert _default_applies(model) is True
        assert model_supports_auto(model) is True
