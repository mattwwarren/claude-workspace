"""Hook management for auto-backgrounding on context exhaustion."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import click

from cw.config import HOOKS_DIR
from cw.exceptions import CwError

if TYPE_CHECKING:
    from pathlib import Path

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


def install_context_hook(client: str, threshold: int) -> Path:
    """Install a PostToolUse hook script for context monitoring.

    Returns the path to the installed script.
    """
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
