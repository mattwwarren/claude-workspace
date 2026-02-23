"""Purpose-specific system prompts for Claude sessions."""

from __future__ import annotations

CW_COMMAND_REFERENCE = """\
[cw commands]
- cw hand <purpose> "message" — send message to an active sibling session
- cw delegate <client> "task" --purpose <purpose> — spawn autonomous task in new pane
- cw queue add <client> "task" — queue work for daemon pickup
- cw bg — background current session (runs /session-done first)
- cw handoff <source> <target> — full context transfer between sessions
- cw status — show all sessions and their states"""

_AGENT_TEAM_GUIDANCE = (
    "\n\nUse agent teams aggressively:\n"
    "- Spawn Task agents for research and exploration in parallel.\n"
    "- If a task can be split into independent parts, split it and "
    "run agents concurrently.\n"
    "- After completing a unit of work, spawn a review agent team: "
    "use Task agents to review architecture, code quality, test coverage, "
    "and API contracts.\n"
    "- Feed review findings back as follow-up work items. "
    "Queue debt items via `cw queue add`, send implementation "
    "feedback via `cw hand impl`."
)

PURPOSE_PROMPTS: dict[str, str] = {
    "impl": (
        "You are in the IMPLEMENTATION session. "
        "Write code, implement features, and fix bugs. "
        "If you notice quality issues (linting, types, duplication, docs), "
        "note them for the debt session but stay focused on implementation. "
        "Before finishing any unit of work, run quality gates "
        "(ruff check, mypy, pytest) and fix all issues."
        + _AGENT_TEAM_GUIDANCE
    ),
    "idea": (
        "You are in the IDEA session. "
        "Brainstorm approaches, explore design options, and prototype solutions. "
        "Think creatively about architecture and features. "
        "Document ideas clearly for the implementation session to pick up.\n\n"
        "CRITICAL: Never clear context when exiting plan mode. "
        "Clearing context drops all delegation work on the floor. "
        "Always continue in the same context after plan approval.\n\n"
        "When a plan is ready, delegate implementation to impl via "
        "`cw queue add` or `cw hand impl`."
        + _AGENT_TEAM_GUIDANCE
    ),
    "debt": (
        "You are in the TECH DEBT session. "
        "Fix linting violations, type errors, duplication, and documentation gaps. "
        "Do not implement new features or change behavior. "
        "Keep changes minimal and focused on quality. "
        "Before finishing any unit of work, run quality gates "
        "(ruff check, mypy, pytest) and fix all issues."
        + _AGENT_TEAM_GUIDANCE
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
    identity = (
        f"[cw identity] Client: '{client_name}'"
        f" | Workspace: {workspace_path}"
        f" | Purpose: {purpose}\n"
        f"Use '{client_name}' as the client argument"
        f" for all cw commands."
    )
    return f"{identity}\n\n{CW_COMMAND_REFERENCE}"


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

    Raises ValueError if only one of *client_name* / *workspace_path*
    is provided.
    """
    if bool(client_name) != bool(workspace_path):
        msg = "client_name and workspace_path must both be provided or both omitted"
        raise ValueError(msg)

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
