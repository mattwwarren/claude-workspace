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


def build_session_context(
    client_name: str,
    workspace_path: str,
    purpose: str,
) -> str:
    """Build an identity preamble for Claude sessions.

    Returns a short block that tells the LLM which client and purpose it
    belongs to, so ``cw`` commands use the correct client argument.
    """
    return (
        f"[cw identity] Client: '{client_name}'"
        f" | Workspace: {workspace_path}"
        f" | Purpose: {purpose}\n"
        f"Use '{client_name}' as the client argument"
        f" for all cw commands (e.g. cw queue add {client_name} ...)."
    )


def get_purpose_prompt(
    purpose: str,
    client_overrides: dict[str, str] | None = None,
    *,
    client_name: str | None = None,
    workspace_path: str | None = None,
) -> str | None:
    """Resolve the system prompt for a given purpose.

    Client overrides take precedence over defaults.
    Returns None if no prompt is defined for the purpose.

    When *client_name* and *workspace_path* are provided, the resolved
    prompt is prefixed with a ``[cw identity]`` block so the LLM knows
    which client/purpose it belongs to.
    """
    if client_overrides and purpose in client_overrides:
        prompt: str | None = client_overrides[purpose]
    else:
        prompt = PURPOSE_PROMPTS.get(purpose)

    if prompt is not None and client_name and workspace_path:
        context = build_session_context(client_name, workspace_path, purpose)
        prompt = f"{context}\n\n{prompt}"

    return prompt


def escape_kdl_string(text: str) -> str:
    """Escape a string for safe embedding in KDL values.

    Handles quotes and backslashes that could break KDL parsing.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')
