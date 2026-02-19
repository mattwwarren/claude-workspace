"""Purpose-specific system prompts for Claude sessions."""

from __future__ import annotations

PURPOSE_PROMPTS: dict[str, str] = {
    "impl": (
        "You are in the IMPLEMENTATION session. "
        "Write code, implement features, and fix bugs. "
        "If you notice quality issues (linting, types, duplication, docs), "
        "note them for the debt session but stay focused on implementation."
    ),
    "review": (
        "You are in the REVIEW session. "
        "Review code for bugs, security issues, and design problems. "
        "Report findings clearly but do not fix them yourself. "
        "Focus on what matters most, not style nitpicks."
    ),
    "debt": (
        "You are in the TECH DEBT session. "
        "Fix linting violations, type errors, duplication, and documentation gaps. "
        "Do not implement new features or change behavior. "
        "Keep changes minimal and focused on quality."
    ),
    "explore": (
        "You are in the EXPLORE session. "
        "Read and analyze the codebase to understand architecture and patterns. "
        "Do not make any changes to files. "
        "Answer questions and provide insights about the code."
    ),
}


def get_purpose_prompt(
    purpose: str,
    client_overrides: dict[str, str] | None = None,
) -> str | None:
    """Resolve the system prompt for a given purpose.

    Client overrides take precedence over defaults.
    Returns None if no prompt is defined for the purpose.
    """
    if client_overrides and purpose in client_overrides:
        return client_overrides[purpose]
    return PURPOSE_PROMPTS.get(purpose)


def escape_kdl_string(text: str) -> str:
    """Escape a string for safe embedding in KDL values.

    Handles quotes and backslashes that could break KDL parsing.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')
