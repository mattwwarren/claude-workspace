"""Hook management for auto-backgrounding and event dispatch."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
from typing import TYPE_CHECKING

import click

from cw.config import HOOKS_DIR
from cw.exceptions import CwError
from cw.models import EventHookRegistry, HookRule

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

_SAFE_CLIENT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

CONTEXT_CHECK_SCRIPT = """\
#!/usr/bin/env bash
# cw context-check hook — auto-background on high turn count
# Installed by: cw hook install <client>
# Threshold configured in ~/.config/cw/clients.yaml

TURN_FILE="${{HOME}}/.local/share/cw/hooks/.turn-count-{client}"
THRESHOLD={threshold}

# Increment turn counter
if [ -f "$TURN_FILE" ]; then
    COUNT=$(cat "$TURN_FILE")
else
    COUNT=0
fi
COUNT=$((COUNT + 1))
echo "$COUNT" > "$TURN_FILE"

# Check threshold
if [ "$COUNT" -ge "$THRESHOLD" ]; then
    echo "[cw] Context threshold reached"
    echo "($COUNT/$THRESHOLD turns). Auto-backgrounding..."
    cw bg --auto 2>/dev/null || true
    echo "0" > "$TURN_FILE"
fi

exit 0
"""


def _hook_script_path(client: str) -> Path:
    return HOOKS_DIR / f"context-check-{client}.sh"


def _turn_count_path(client: str) -> Path:
    return HOOKS_DIR / f".turn-count-{client}"


def _validate_client_name(client: str) -> None:
    """Reject client names with shell metacharacters."""
    if not _SAFE_CLIENT_RE.match(client):
        msg = (
            f"Unsafe client name: {client!r}. "
            "Use only alphanumeric characters, hyphens, dots, and underscores."
        )
        raise CwError(msg)


def install_context_hook(client: str, threshold: int) -> Path:
    """Install a PostToolUse hook script for context monitoring.

    Returns the path to the installed script.
    """
    _validate_client_name(client)
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    script_path = _hook_script_path(client)
    script_content = CONTEXT_CHECK_SCRIPT.format(
        client=client,
        threshold=threshold,
    )
    script_path.write_text(script_content)
    script_path.chmod(0o755)
    click.echo(f"Installed context hook: {script_path}")
    click.echo(f"Threshold: {threshold} turns")
    return script_path


def uninstall_context_hook(client: str) -> None:
    """Remove the context monitoring hook for a client."""
    script_path = _hook_script_path(client)
    if not script_path.exists():
        msg = f"No hook installed for client '{client}'."
        raise CwError(msg)
    script_path.unlink()
    # Also clean up turn counter
    turn_path = _turn_count_path(client)
    if turn_path.exists():
        turn_path.unlink()
    click.echo(f"Uninstalled context hook for {client}.")


def hook_status(client: str) -> dict[str, object]:
    """Check hook installation status for a client."""
    script_path = _hook_script_path(client)
    turn_path = _turn_count_path(client)
    installed = script_path.exists()
    turn_count = 0
    if turn_path.exists():
        with contextlib.suppress(ValueError, OSError):
            turn_count = int(turn_path.read_text().strip())
    return {
        "installed": installed,
        "script_path": str(script_path),
        "turn_count": turn_count,
    }


def reset_turn_count(client: str) -> None:
    """Reset the turn counter for a client."""
    turn_path = _turn_count_path(client)
    if turn_path.exists():
        turn_path.write_text("0")


# ---------------------------------------------------------------------------
# Event hook registry — user-defined shell commands for lifecycle events
# ---------------------------------------------------------------------------


def _event_hook_registry_path(client: str) -> Path:
    """Return the JSON registry path for a client's event hooks."""
    return HOOKS_DIR / f"event-hooks-{client}.json"


def load_event_hooks(client: str) -> EventHookRegistry:
    """Load event hook rules for a client from disk."""
    path = _event_hook_registry_path(client)
    if not path.exists():
        return EventHookRegistry()
    raw = json.loads(path.read_text())
    return EventHookRegistry.model_validate(raw)


def save_event_hooks(client: str, registry: EventHookRegistry) -> None:
    """Persist event hook rules for a client to disk."""
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    path = _event_hook_registry_path(client)
    path.write_text(registry.model_dump_json(indent=2))


def add_event_hook(
    client: str,
    event_type: str,
    command: str,
    *,
    description: str = "",
) -> HookRule:
    """Add an event hook rule for a client."""
    registry = load_event_hooks(client)
    rule = HookRule(
        event_type=event_type,
        command=command,
        description=description,
    )
    registry.rules.append(rule)
    save_event_hooks(client, registry)
    return rule


def remove_event_hook(client: str, event_type: str) -> int:
    """Remove all event hook rules matching an event type.

    Returns the number of rules removed.
    """
    registry = load_event_hooks(client)
    original_count = len(registry.rules)
    registry.rules = [r for r in registry.rules if r.event_type != event_type]
    removed = original_count - len(registry.rules)
    if removed > 0:
        save_event_hooks(client, registry)
    return removed


def list_event_hooks(client: str) -> list[HookRule]:
    """List all event hook rules for a client."""
    return load_event_hooks(client).rules


def dispatch_event_hooks(
    client: str,
    event_type: str,
    metadata: dict[str, str] | None = None,
) -> None:
    """Fire matching event hooks as fire-and-forget subprocesses.

    Hooks must never break the caller — all exceptions are caught and logged.
    Environment variables are set for the hook command:
    - CW_CLIENT: the client name
    - CW_EVENT_TYPE: the event type string
    - CW_META_<KEY>: one per metadata entry (uppercased key)
    """
    try:
        registry = load_event_hooks(client)
    except Exception:
        log.debug("Failed to load event hooks for %s", client, exc_info=True)
        return

    matching = [r for r in registry.rules if r.event_type == event_type]
    if not matching:
        return

    env = os.environ.copy()
    env["CW_CLIENT"] = client
    env["CW_EVENT_TYPE"] = event_type
    if metadata:
        for key, value in metadata.items():
            env[f"CW_META_{key.upper()}"] = value

    for rule in matching:
        try:
            subprocess.Popen(
                ["/bin/sh", "-c", rule.command],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            log.debug(
                "Failed to dispatch hook %r for %s/%s",
                rule.command,
                client,
                event_type,
                exc_info=True,
            )
