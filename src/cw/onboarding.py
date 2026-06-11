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
# Why: mirrors _DEFAULT_BASE_URL in cw_queue_events_channel.py — update
# together if port changes.
_QUEUE_EVENTS_BASE_URL = "http://127.0.0.1:8789"
# Why: mirrors _DEFAULT_BASE_URL in cw_pr_events_channel.py — update
# together if port changes.
_PR_EVENTS_BASE_URL = "http://127.0.0.1:8788"

# Marker inserted into CLAUDE.md to detect already-onboarded repos.
_CLAUDE_MD_MARKER = "<!-- cw-onboarding -->"


def register_mcp_servers(workspace_path: Path, client_name: str) -> bool:
    """Merge cw-queue-events and cw-pr-events into ``<workspace>/.mcp.json``.

    Existing entries are preserved (skip-if-present semantics per key).
    Absent file is treated as ``{}``. Writes are atomic.
    Unparseable existing file → emit manual instruction, leave untouched.

    Returns True if anything was written, False if already configured.
    """
    mcp_path = workspace_path / ".mcp.json"

    existing: dict[str, Any] = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            click.echo(
                f"cw init: could not parse {mcp_path} — "
                'add "cw-queue-events" and "cw-pr-events" to mcpServers manually.'
            )
            return False
        except OSError:
            existing = {}

    servers: dict[str, Any] = existing.setdefault("mcpServers", {})

    changed = False
    if "cw-queue-events" not in servers:
        servers["cw-queue-events"] = {
            "command": "cw",
            "args": ["queue-channel", "proxy", "--client-id", client_name],
            "env": {"CW_QUEUE_EVENTS_BASE_URL": _QUEUE_EVENTS_BASE_URL},
        }
        changed = True

    if "cw-pr-events" not in servers:
        servers["cw-pr-events"] = {
            "command": "cw",
            "args": ["pr-channel", "proxy", "--client-id", client_name],
            "env": {"CW_PR_EVENTS_BASE_URL": _PR_EVENTS_BASE_URL},
        }
        changed = True

    if changed:
        atomic_write_text(mcp_path, json.dumps(existing, indent=2) + "\n")

    return changed


CW_ALLOWLIST_ENTRY = "Bash(cw:*)"


def install_cw_allowlist() -> bool:
    """Merge ``Bash(cw:*)`` into ``~/.claude/settings.json`` allow list.

    Skip-if-present. Unparseable JSON → emit manual instruction, return early
    (no raise, no write).

    Returns True if anything was written, False if already configured.
    """
    raw = ""
    if _CLAUDE_SETTINGS_PATH.exists():
        try:
            raw = _CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8")
        except OSError:
            raw = ""

    data: dict[str, Any] = {}
    if raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            click.echo(
                "cw init: could not parse ~/.claude/settings.json — "
                f'add "{CW_ALLOWLIST_ENTRY}" to permissions.allow manually.'
            )
            return False

    permissions: dict[str, Any] = data.setdefault("permissions", {})
    allow: list[Any] = permissions.setdefault("allow", [])

    if CW_ALLOWLIST_ENTRY in allow:
        return False

    allow.append(CW_ALLOWLIST_ENTRY)
    _CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_CLAUDE_SETTINGS_PATH, json.dumps(data, indent=2) + "\n")
    return True


_SESSIONSTART_COMMAND = "cw orchestrate status --json || true"


def install_sessionstart_hook(workspace_path: Path) -> bool:
    """Add the ``cw orchestrate status`` SessionStart hook to the workspace settings.

    Target: ``<workspace>/.claude/settings.json`` (NOT ``.local.json``).
    Duplicate-detection traverses hooks.SessionStart entries; skip-if-present.
    Creates ``<workspace>/.claude/`` if absent.

    Returns True if anything was written, False if already configured.
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
            click.echo(
                f"cw init: could not parse {settings_path} — "
                "resetting to empty settings to add SessionStart hook."
            )
            data = {}

    # Duplicate detection: traverse hooks.SessionStart[*].hooks[*].command
    hooks: dict[str, Any] = data.setdefault("hooks", {})
    for item in hooks.get("SessionStart", []):
        for hook in item.get("hooks", []):
            if hook.get("command") == _SESSIONSTART_COMMAND:
                return False  # already present

    # Build or extend the hooks structure.
    session_start: list[Any] = hooks.setdefault("SessionStart", [])
    session_start.append(
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": _SESSIONSTART_COMMAND}],
        }
    )

    atomic_write_text(settings_path, json.dumps(data, indent=2) + "\n")
    return True


def install_claude_md_snippet(workspace_path: Path) -> bool:
    """Append a cw-onboarding snippet to ``<workspace>/.claude/CLAUDE.md``.

    Always writes the snippet body (idempotent via the ``<!-- cw-onboarding -->``
    marker). Runs ``cw schema list`` as a probe; on non-zero exit, schema lines
    are omitted from the snippet but the snippet is still written.
    Creates ``<workspace>/.claude/`` if absent.

    Returns True if anything was written, False if already configured.
    """
    schema_ok = (
        subprocess.run(
            ["cw", "schema", "list"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )

    claude_dir = workspace_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_md = claude_dir / "CLAUDE.md"

    existing_text = ""
    if claude_md.exists():
        try:
            existing_text = claude_md.read_text(encoding="utf-8")
        except OSError:
            existing_text = ""

    if _CLAUDE_MD_MARKER in existing_text:
        return False  # already onboarded

    queue_port = _QUEUE_EVENTS_BASE_URL.rsplit(":", 1)[-1]
    pr_port = _PR_EVENTS_BASE_URL.rsplit(":", 1)[-1]
    schema_lines = (
        "- Run `cw schema <command>` for machine-readable output schemas;\n"
        "  most cw commands accept `--json`.\n"
        "- Example: `cw schema list` shows all available schemas.\n"
    )
    snippet = (
        "\n"
        f"{_CLAUDE_MD_MARKER}\n"
        "## cw Agent Integration\n"
        "\n"
        "This workspace is managed by `cw`. Background sessions receive tasks\n"
        "via the MCP channels wired in `.mcp.json`.\n"
        "\n"
        f"- Queue events: `cw-queue-events` MCP server (port {queue_port})\n"
        f"- PR events: `cw-pr-events` MCP server (port {pr_port})\n"
        "- Dispatch status: `cw orchestrate status` (SessionStart hook)\n"
        + (schema_lines if schema_ok else "")
    )

    try:
        atomic_write_text(claude_md, existing_text + snippet)
    except OSError as exc:
        click.echo(f"cw init: could not write .claude/CLAUDE.md snippet: {exc}")
        return False
    return True
