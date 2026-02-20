"""Tests for cw.prompts - Purpose-specific system prompts."""

from __future__ import annotations

from cw.prompts import (
    PURPOSE_PROMPTS,
    build_session_context,
    escape_kdl_string,
    get_purpose_prompt,
)


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

    def test_with_client_context_prepends_identity(self) -> None:
        prompt = get_purpose_prompt(
            "impl",
            client_name="personal",
            workspace_path="/home/user/workspace",
        )
        assert prompt is not None
        assert prompt.startswith("[cw identity]")
        assert "personal" in prompt
        assert "/home/user/workspace" in prompt
        # Original prompt still present after the context
        assert "IMPLEMENTATION" in prompt

    def test_without_client_context_unchanged(self) -> None:
        prompt = get_purpose_prompt("impl")
        assert prompt == PURPOSE_PROMPTS["impl"]

    def test_client_context_with_override(self) -> None:
        overrides = {"review": "HIPAA review."}
        prompt = get_purpose_prompt(
            "review",
            overrides,
            client_name="health",
            workspace_path="/opt/health",
        )
        assert prompt is not None
        assert "[cw identity]" in prompt
        assert "HIPAA review." in prompt

    def test_unknown_purpose_with_context_still_none(self) -> None:
        prompt = get_purpose_prompt(
            "unknown",
            client_name="test",
            workspace_path="/opt/test",
        )
        assert prompt is None


class TestBuildSessionContext:
    def test_returns_expected_format(self) -> None:
        result = build_session_context("personal", "/home/user/ws", "review")
        assert "[cw identity]" in result
        assert "Client: 'personal'" in result
        assert "Workspace: /home/user/ws" in result
        assert "Purpose: review" in result
        assert "cw queue add personal" in result

    def test_client_name_in_usage_hint(self) -> None:
        result = build_session_context("my-proj", "/opt/proj", "impl")
        assert "cw queue add my-proj" in result


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
