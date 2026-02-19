"""Tests for cw.prompts - Purpose-specific system prompts."""

from __future__ import annotations

from cw.prompts import PURPOSE_PROMPTS, escape_kdl_string, get_purpose_prompt


class TestGetPurposePrompt:
    def test_default_impl_prompt(self) -> None:
        prompt = get_purpose_prompt("impl")
        assert prompt is not None
        assert "IMPLEMENTATION" in prompt

    def test_default_review_prompt(self) -> None:
        prompt = get_purpose_prompt("review")
        assert prompt is not None
        assert "REVIEW" in prompt

    def test_default_debt_prompt(self) -> None:
        prompt = get_purpose_prompt("debt")
        assert prompt is not None
        assert "TECH DEBT" in prompt

    def test_default_explore_prompt(self) -> None:
        prompt = get_purpose_prompt("explore")
        assert prompt is not None
        assert "EXPLORE" in prompt

    def test_unknown_purpose_returns_none(self) -> None:
        assert get_purpose_prompt("unknown") is None

    def test_client_override_takes_precedence(self) -> None:
        overrides = {"review": "Review with HIPAA focus."}
        prompt = get_purpose_prompt("review", overrides)
        assert prompt == "Review with HIPAA focus."

    def test_client_override_none_falls_through(self) -> None:
        prompt = get_purpose_prompt("impl", None)
        assert prompt == PURPOSE_PROMPTS["impl"]

    def test_client_override_only_for_specified_purpose(self) -> None:
        overrides = {"review": "Custom review."}
        # impl should still use default
        prompt = get_purpose_prompt("impl", overrides)
        assert prompt == PURPOSE_PROMPTS["impl"]


class TestEscapeKdlString:
    def test_quotes_escaped(self) -> None:
        assert escape_kdl_string('say "hello"') == 'say \\"hello\\"'

    def test_backslash_escaped(self) -> None:
        assert escape_kdl_string("path\\to") == "path\\\\to"

    def test_no_special_chars(self) -> None:
        text = "simple text"
        assert escape_kdl_string(text) == text

    def test_mixed_special_chars(self) -> None:
        text = 'a "b" c\\d'
        assert escape_kdl_string(text) == 'a \\"b\\" c\\\\d'
