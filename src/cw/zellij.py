"""Zellij terminal multiplexer integration."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from jinja2 import DictLoader, Environment

from cw.exceptions import ZellijError
from cw.models import ClientConfig

GENERATED_LAYOUTS_DIR = Path.home() / ".config" / "zellij" / "layouts"

CLIENT_LAYOUT_TEMPLATE = """\
layout {
    pane size=1 borderless=true {
        plugin location="tab-bar"
    }
    pane split_direction="vertical" {
        pane size="20%" name="files" {
            command "yazi"
            args "{{ workspace_path }}"
        }
        pane split_direction="horizontal" size="80%" {
            pane size="70%" name="impl" focus=true {
                cwd "{{ workspace_path }}"
                command "claude"
                args {{ panes.impl.claude_args }}
            }
            pane split_direction="vertical" size="30%" {
                pane name="review" {
                    cwd "{{ workspace_path }}"
                    command "claude"
                    args {{ panes.review.claude_args }}
                }
                pane name="debt" {
                    cwd "{{ workspace_path }}"
                    command "claude"
                    args {{ panes.debt.claude_args }}
                }
            }
        }
    }
    pane size=1 borderless=true {
        plugin location="status-bar"
    }
}
"""


_env = Environment(
    loader=DictLoader({"client.kdl.j2": CLIENT_LAYOUT_TEMPLATE}),
    keep_trailing_newline=True,
)


def _run_zellij(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a zellij command, raising ZellijError on failure if check=True."""
    cmd = ["zellij", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=check)
    except subprocess.CalledProcessError as e:
        msg = f"Zellij command failed: {' '.join(cmd)}"
        raise ZellijError(msg) from e


def is_installed() -> bool:
    """Check if zellij is available on PATH."""
    return shutil.which("zellij") is not None


def list_sessions() -> list[str]:
    """List running Zellij sessions."""
    result = _run_zellij("list-sessions", "--no-formatting", check=False)
    if result.returncode != 0:
        return []
    return [
        line.strip().split()[0]
        for line in result.stdout.strip().splitlines()
        if line.strip()
    ]


def session_exists(session_name: str) -> bool:
    """Check if a Zellij session with the given name exists."""
    return session_name in list_sessions()


def generate_layout(
    client: ClientConfig,
    panes: dict[str, dict[str, str]] | None = None,
) -> Path:
    """Render the layout template for a client, returning the output path.

    Args:
        client: Client configuration.
        panes: Optional per-pane config. Each key is a pane name (impl, review, debt)
               with a dict containing 'claude_args' (pre-formatted KDL args string).
    """
    GENERATED_LAYOUTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = GENERATED_LAYOUTS_DIR / f"cw-{client.name}.kdl"

    # Default: fresh claude sessions with no special args
    if panes is None:
        panes = {
            name: {"claude_args": '""'}
            for name in ("impl", "review", "debt")
        }

    template = _env.get_template("client.kdl.j2")
    rendered = template.render(
        client_name=client.name,
        workspace_path=str(client.workspace_path),
        panes=panes,
    )
    output_path.write_text(rendered)
    return output_path


def create_and_attach(session_name: str, layout_path: Path) -> None:
    """Create a new Zellij session and attach to it.

    This takes over the current terminal. The user lands directly in the session.
    Uses --new-session-with-layout to explicitly create (not attach to existing).
    """
    subprocess.run(
        [
            "zellij",
            "--new-session-with-layout",
            str(layout_path),
            "--session",
            session_name,
        ],
        check=False,
    )


def attach_session(session_name: str) -> None:
    """Attach to an existing Zellij session (takes over terminal)."""
    subprocess.run(["zellij", "attach", session_name], check=False)


def write_to_pane(text: str, session: str | None = None) -> None:
    """Write text to the currently focused Zellij pane.

    Args:
        text: Text to inject as keystrokes.
        session: Target a specific session by name (for remote control).
                 If None, targets the current session (must be inside one).
    """
    if session:
        _run_zellij("-s", session, "action", "write-chars", text)
    else:
        _run_zellij("action", "write-chars", text)


def go_to_tab(tab_name: str, session: str | None = None) -> None:
    """Switch to a named tab in the current Zellij session."""
    if session:
        args = ["-s", session, "action", "go-to-tab-name", tab_name]
    else:
        args = ["action", "go-to-tab-name", tab_name]
    result = _run_zellij(*args, check=False)
    if result.returncode != 0:
        msg = f"Could not switch to tab '{tab_name}': {result.stderr.strip()}"
        raise ZellijError(msg)


def _get_focused_pane_id(session: str | None = None) -> str | None:
    """Get the ZELLIJ_PANE_ID of the currently focused pane."""
    base = ["-s", session] if session else []
    result = _run_zellij(*base, "action", "list-clients", check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.strip().splitlines():
        if line.startswith("CLIENT_ID"):
            continue
        parts = line.split(None, 2)
        if len(parts) >= 2:
            return parts[1]  # e.g. "terminal_2"
    return None


def _get_pane_id_for_name(
    pane_name: str,
    session: str | None = None,
) -> str | None:
    """Map a pane name to its terminal_N id via dump-layout.

    Only looks at the first tab block (skips new_tab_template).
    """
    base = ["-s", session] if session else []
    result = _run_zellij(*base, "action", "dump-layout", check=False)
    if result.returncode != 0:
        return None

    # Track terminal index: panes appear in dump-layout in terminal order
    # terminal_0 = first command pane (files/yazi)
    # terminal_1 = impl, terminal_2 = review, terminal_3 = debt
    terminal_idx = 0
    in_first_tab = False
    for line in result.stdout.splitlines():
        if "tab " in line and "name=" in line:
            if in_first_tab:
                break  # Hit second tab (new_tab_template), stop
            in_first_tab = True
            continue
        if not in_first_tab:
            continue
        # Match panes with commands (these are terminal panes)
        if re.search(r'pane\b.*\bcommand=', line):
            match = re.search(r'name="([^"]+)"', line)
            if match and match.group(1) == pane_name:
                return f"terminal_{terminal_idx}"
            terminal_idx += 1
    return None


# Max panes to cycle through before giving up
_MAX_PANE_CYCLE = 10


def focus_pane(pane_name: str, session: str | None = None) -> None:
    """Focus a pane by name by cycling focus-next-pane.

    Uses list-clients (reliable source of truth) to check which
    pane is focused, and dump-layout to map pane names to terminal IDs.
    """
    target_id = _get_pane_id_for_name(pane_name, session)
    if target_id is None:
        msg = f"Pane '{pane_name}' not found in layout."
        raise ZellijError(msg)

    current_id = _get_focused_pane_id(session)
    if current_id == target_id:
        return  # Already there

    base = ["-s", session] if session else []
    for _ in range(_MAX_PANE_CYCLE):
        _run_zellij(*base, "action", "focus-next-pane", check=False)
        current_id = _get_focused_pane_id(session)
        if current_id == target_id:
            return

    msg = f"Could not focus pane '{pane_name}' after cycling."
    raise ZellijError(msg)


def check_pane_health(session: str | None = None) -> dict[str, bool]:
    """Check which named panes have running commands.

    Parses dump-layout to find panes with active command= processes.
    Returns a dict mapping pane name to whether its command is still running.

    A pane that exists in the layout with a command= attribute is considered alive.
    If the command has exited (pane shows as 'exited' or has no command), it's dead.
    """
    base = ["-s", session] if session else []
    result = _run_zellij(*base, "action", "dump-layout", check=False)
    if result.returncode != 0:
        return {}

    health: dict[str, bool] = {}
    in_first_tab = False
    for line in result.stdout.splitlines():
        if "tab " in line and "name=" in line:
            if in_first_tab:
                break
            in_first_tab = True
            continue
        if not in_first_tab:
            continue
        # Match panes with names
        name_match = re.search(r'name="([^"]+)"', line)
        if not name_match:
            continue
        pane_name = name_match.group(1)
        # A pane with command= and no "exited" marker is alive
        has_command = bool(re.search(r'\bcommand=', line))
        is_exited = "exited" in line.lower()
        health[pane_name] = has_command and not is_exited

    return health


def in_zellij_session() -> bool:
    """Check if we're currently running inside a Zellij session."""
    return "ZELLIJ_SESSION_NAME" in os.environ


def current_session_name() -> str | None:
    """Get the name of the current Zellij session, if inside one."""
    return os.environ.get("ZELLIJ_SESSION_NAME")
