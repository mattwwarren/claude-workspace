"""Purpose-specific system prompts for Claude sessions."""

from __future__ import annotations

CW_COMMAND_REFERENCE = """\
[cw commands]
- cw dev-queue add <ticket> — enqueue a ticket for the auto-dev pipeline
- cw dev-queue status — show dev-queue tickets and their stages
- cw queue peek — inspect running dev-queue sessions (read-only)
- cw bg — background current session (runs /session-done first)
- cw status — show all sessions and their states"""

_AGENT_TEAM_GUIDANCE = (
    "\n\nUse agent teams aggressively:\n"
    "- Spawn Task agents for research and exploration in parallel.\n"
    "- If a task can be split into independent parts, split it and "
    "run agents concurrently.\n"
    "- After completing a unit of work, spawn a review agent team: "
    "use Task agents to review architecture, code quality, test coverage, "
    "and API contracts.\n"
    "- Feed review findings back as follow-up work items."
)

_DEFAULT_QUALITY_GATES = "ruff check, mypy, pytest"

_IMPL_PROMPT_BASE = (
    "You are in the IMPLEMENTATION session. "
    "Write code, implement features, and fix bugs. "
    "If you notice quality issues (linting, types, duplication, docs), "
    "note them for later cleanup but stay focused on implementation. "
)

_DEBT_PROMPT_BASE = (
    "You are in the TECH DEBT session. "
    "Fix linting violations, type errors, duplication, and documentation gaps. "
    "Do not implement new features or change behavior. "
    "Keep changes minimal and focused on quality. "
)


def _quality_gate_sentence(commands: str) -> str:
    """Render the gate sentence naming *commands* as the gate list."""
    return (
        "Before finishing any unit of work, run quality gates "
        f"({commands}) and fix all issues."
    )


# Purposes whose prompt carries a quality-gate sentence, keyed to the base text
# the sentence is appended to. "idea" is absent: it has no gate sentence.
_GATED_PROMPT_BASES: dict[str, str] = {
    "impl": _IMPL_PROMPT_BASE,
    "debt": _DEBT_PROMPT_BASE,
}

PURPOSE_PROMPTS: dict[str, str] = {
    "impl": (
        _IMPL_PROMPT_BASE
        + _quality_gate_sentence(_DEFAULT_QUALITY_GATES)
        + _AGENT_TEAM_GUIDANCE
    ),
    "idea": (
        "You are in the IDEA session. "
        "Brainstorm approaches, explore design options, and prototype solutions. "
        "Think creatively about architecture and features. "
        "Document ideas clearly for the implementation session to pick up.\n\n"
        "CRITICAL: Never clear context when exiting plan mode. "
        "Clearing context drops all delegation work on the floor. "
        "Always continue in the same context after plan approval."
        + _AGENT_TEAM_GUIDANCE
    ),
    "debt": (
        _DEBT_PROMPT_BASE
        + _quality_gate_sentence(_DEFAULT_QUALITY_GATES)
        + _AGENT_TEAM_GUIDANCE
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
    quality_gate_commands: str | None = None,
) -> str | None:
    """Resolve the system prompt for a given purpose.

    Client overrides take precedence over defaults.
    Returns None if no prompt is defined for the purpose.

    When *client_name* and *workspace_path* are provided, the resolved
    prompt is prefixed with a ``[cw identity]`` block so the LLM knows
    which client/purpose it belongs to.

    *quality_gate_commands* replaces the gate list named in the ``impl`` and
    ``debt`` prompts, for clients whose stack is not the Python default:

    - ``None`` (default): keep the default ``ruff check, mypy, pytest`` triad.
    - ``""``: omit the gate sentence entirely.
    - any other string: substitute it verbatim into the gate sentence.

    It has no effect on ``idea`` (no gate sentence) and is superseded by a
    whole-prompt entry in *client_overrides*.

    Raises ValueError if only one of *client_name* / *workspace_path*
    is provided.
    """
    if bool(client_name) != bool(workspace_path):
        msg = "client_name and workspace_path must both be provided or both omitted"
        raise ValueError(msg)

    if client_overrides and purpose in client_overrides:
        prompt: str | None = client_overrides[purpose]
    elif purpose in _GATED_PROMPT_BASES and quality_gate_commands is not None:
        gate_sentence = (
            _quality_gate_sentence(quality_gate_commands)
            if quality_gate_commands
            else ""
        )
        prompt = _GATED_PROMPT_BASES[purpose] + gate_sentence + _AGENT_TEAM_GUIDANCE
    else:
        prompt = PURPOSE_PROMPTS.get(purpose)

    if prompt is not None and client_name and workspace_path:
        context = build_session_context(client_name, workspace_path, purpose)
        prompt = f"{context}\n\n{prompt}"

    return prompt
