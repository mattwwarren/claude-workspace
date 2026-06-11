"""Agent-onboarding helpers wired into ``cw init``.

Each function is idempotent — safe to call multiple times. Skip-if-present
semantics avoid clobbering user customisations on re-runs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import click

from cw.atomic import atomic_write_text

# Path to Claude Code user settings — enables test monkeypatching without
# module-local patching. Mirrors the pattern in doctor.py:93.
_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Base URLs for the two MCP proxy channels.
_QUEUE_EVENTS_BASE_URL = "http://127.0.0.1:8789"
_PR_EVENTS_BASE_URL = "http://127.0.0.1:8788"

# Marker inserted into CLAUDE.md to detect already-onboarded repos.
_CLAUDE_MD_MARKER = "<!-- cw-onboarding -->"


def register_mcp_servers(workspace_path: Path, client_name: str) -> None:
    """Merge cw-queue-events and cw-pr-events into ``<workspace>/.mcp.json``.

    Existing entries are preserved (skip-if-present semantics per key).
    Absent file is treated as ``{}``. Writes are atomic.
    """
    mcp_path = workspace_path / ".mcp.json"

    existing: dict[str, Any] = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    servers: dict[str, Any] = existing.setdefault("mcpServers", {})

    if "cw-queue-events" not in servers:
        servers["cw-queue-events"] = {
            "command": "cw",
            "args": ["queue-channel", "proxy", "--client-id", client_name],
            "env": {"CW_QUEUE_EVENTS_BASE_URL": _QUEUE_EVENTS_BASE_URL},
        }

    if "cw-pr-events" not in servers:
        servers["cw-pr-events"] = {
            "command": "cw",
            "args": ["pr-channel", "proxy", "--client-id", client_name],
            "env": {"CW_PR_EVENTS_BASE_URL": _PR_EVENTS_BASE_URL},
        }

    atomic_write_text(mcp_path, json.dumps(existing, indent=2) + "\n")


def install_cw_allowlist() -> None:
    """Merge ``Bash(cw:*)`` into ``~/.claude/settings.json`` allow list.

    Skip-if-present. Unparseable JSON → emit manual instruction, return early
    (no raise, no write).
    """
    path = _CLAUDE_SETTINGS_PATH

    raw = ""
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            raw = ""

    data: dict[str, Any] = {}
    if raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            click.echo(
                "cw init: could not parse ~/.claude/settings.json — "
                'add "Bash(cw:*)" to permissions.allow manually.'
            )
            return

    permissions: dict[str, Any] = data.setdefault("permissions", {})
    allow: list[Any] = permissions.setdefault("allow", [])

    entry = "Bash(cw:*)"
    if entry not in allow:
        allow.append(entry)

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def install_sessionstart_hook(workspace_path: Path) -> None:
    """Add the ``cw orchestrate status`` SessionStart hook to the workspace settings.

    Target: ``<workspace>/.claude/settings.json`` (NOT ``.local.json``).
    Duplicate-detection traverses hooks.SessionStart entries; skip-if-present.
    Creates ``<workspace>/.claude/`` if absent.
    """
    claude_dir = workspace_path / ".claude"
    settings_path = claude_dir / "settings.json"

    claude_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if settings_path.exists():
        try:
            raw = settings_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            data = {}

    target_command = "cw orchestrate status --json || true"

    # Duplicate detection: traverse hooks.SessionStart[*].hooks[*].command
    hooks_root: dict[str, Any] = data.get("hooks", {})
    for item in hooks_root.get("SessionStart", []):
        for hook in item.get("hooks", []):
            if hook.get("command") == target_command:
                return  # already present

    # Build or extend the hooks structure.
    hooks_section: dict[str, Any] = data.setdefault("hooks", {})
    session_start: list[Any] = hooks_section.setdefault("SessionStart", [])
    session_start.append(
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": target_command}],
        }
    )

    atomic_write_text(settings_path, json.dumps(data, indent=2) + "\n")


def install_claude_md_snippet(workspace_path: Path) -> None:
    """Append a cw-onboarding snippet to ``<workspace>/CLAUDE.md``.

    Runs ``cw schema list`` first; if it fails (cw schema not available),
    emits a warning and returns. Idempotent via the ``<!-- cw-onboarding -->``
    marker.
    """
    result = subprocess.run(
        ["cw", "schema", "list"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        click.echo("cw init: cw schema list unavailable — skipping CLAUDE.md snippet.")
        return

    claude_md = workspace_path / "CLAUDE.md"

    existing_text = ""
    if claude_md.exists():
        try:
            existing_text = claude_md.read_text(encoding="utf-8")
        except OSError:
            existing_text = ""

    if _CLAUDE_MD_MARKER in existing_text:
        return  # already onboarded

    snippet = (
        "\n"
        f"{_CLAUDE_MD_MARKER}\n"
        "## cw Agent Integration\n"
        "\n"
        "This workspace is managed by `cw`. Background sessions receive tasks\n"
        "via the MCP channels wired in `.mcp.json`.\n"
        "\n"
        "- Queue events: `cw-queue-events` MCP server (port 8789)\n"
        "- PR events: `cw-pr-events` MCP server (port 8788)\n"
        "- Dispatch status: `cw orchestrate status` (SessionStart hook)\n"
    )

    try:
        with claude_md.open("a", encoding="utf-8") as fh:
            fh.write(snippet)
    except OSError as exc:
        click.echo(f"cw init: could not write CLAUDE.md snippet: {exc}")
