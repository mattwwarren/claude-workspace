"""Tests for cw.prompts - Purpose-specific system prompts."""

from __future__ import annotations

import pytest

from cw.prompts import (
    CW_COMMAND_REFERENCE,
    PURPOSE_PROMPTS,
    build_session_context,
    get_purpose_prompt,
)


class TestGetPurposePrompt:
    def test_default_impl_prompt(self) -> None:
        prompt = get_purpose_prompt("impl")
        assert prompt is not None
        assert "IMPLEMENTATION" in prompt

    def test_default_idea_prompt(self) -> None:
        prompt = get_purpose_prompt("idea")
        assert prompt is not None
        assert "IDEA" in prompt

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
        overrides = {"idea": "Idea with HIPAA focus."}
        prompt = get_purpose_prompt("idea", overrides)
        assert prompt == "Idea with HIPAA focus."

    def test_client_override_none_falls_through(self) -> None:
        prompt = get_purpose_prompt("impl", None)
        assert prompt == PURPOSE_PROMPTS["impl"]

    def test_client_override_only_for_specified_purpose(self) -> None:
        overrides = {"idea": "Custom idea."}
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
        overrides = {"idea": "HIPAA idea."}
        prompt = get_purpose_prompt(
            "idea",
            overrides,
            client_name="health",
            workspace_path="/opt/health",
        )
        assert prompt is not None
        assert "[cw identity]" in prompt
        assert "HIPAA idea." in prompt

    def test_unknown_purpose_with_context_still_none(self) -> None:
        prompt = get_purpose_prompt(
            "unknown",
            client_name="test",
            workspace_path="/opt/test",
        )
        assert prompt is None

    def test_partial_kwargs_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="both be provided"):
            get_purpose_prompt("impl", client_name="foo")

        with pytest.raises(ValueError, match="both be provided"):
            get_purpose_prompt("impl", workspace_path="/opt/foo")


class TestBuildSessionContext:
    def test_returns_expected_format(self) -> None:
        result = build_session_context("personal", "/home/user/ws", "idea")
        assert "[cw identity]" in result
        assert "Client: 'personal'" in result
        assert "Workspace: /home/user/ws" in result
        assert "Purpose: idea" in result
        assert "[cw commands]" in result

    def test_client_name_in_usage_hint(self) -> None:
        result = build_session_context("my-proj", "/opt/proj", "impl")
        assert "my-proj" in result

    def test_includes_command_reference(self) -> None:
        result = build_session_context("personal", "/home/user/ws", "impl")
        assert "[cw commands]" in result
        assert "cw queue add" in result
        assert "cw bg" in result
        assert "cw status" in result

    def test_command_reference_constant_not_empty(self) -> None:
        assert CW_COMMAND_REFERENCE
        assert "cw queue add" in CW_COMMAND_REFERENCE
