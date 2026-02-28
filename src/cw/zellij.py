"""Zellij terminal multiplexer integration."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import DictLoader, Environment

from cw.exceptions import ZellijError

_PLUGIN_INSTALL_DIR = Path.home() / ".config" / "zellij" / "plugins"
_PLUGIN_FILENAME = "cw_status.wasm"

if TYPE_CHECKING:
    from cw.models import ClientConfig

GENERATED_LAYOUTS_DIR = Path.home() / ".config" / "zellij" / "layouts"

CLIENT_LAYOUT_TEMPLATE = """\
layout {
{%- if session_mode %}
    default_tab_template {
        pane size=1 borderless=true {
            plugin location="tab-bar"
        }
        children
{%- if cw_plugin_path %}
        pane size=1 borderless=true {
            plugin location="file:{{ cw_plugin_path }}"
        }
{%- endif %}
        pane size=1 borderless=true {
            plugin location="status-bar"
        }
    }
{%- endif %}
    tab name="{{ client_name }}" {
        pane split_direction="vertical" {
            pane split_direction="horizontal" size="{{ primary_size }}" {
                pane name="{{ primary_pane.name }}" focus=true {
                    cwd "{{ primary_pane.cwd }}"
                    command "bash"
                    args "-c" {{ primary_pane.claude_cmd }}
                }
                pane size="10%" name="terminal" {
                    cwd "{{ workspace_path }}"
                    command "bash"
                    args "-c" "cw daemon start"
                }
            }
{%- if secondary_panes %}
            pane split_direction="horizontal" \
size="{{ secondary_size }}" {
{%- for pane in secondary_panes %}
                pane name="{{ pane.name }}" {
                    cwd "{{ pane.cwd }}"
                    command "bash"
                    args "-c" {{ pane.claude_cmd }}
                }
{%- endfor %}
            }
{%- endif %}
        }
    }
}
"""

CW_PLUGIN_PATH = _PLUGIN_INSTALL_DIR / _PLUGIN_FILENAME


_env = Environment(
    loader=DictLoader({"client.kdl.j2": CLIENT_LAYOUT_TEMPLATE}),
    keep_trailing_newline=True,
    autoescape=False,  # KDL config templates, not HTML - no XSS risk
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
    """List running Zellij sessions (excludes EXITED sessions)."""
    result = _run_zellij("list-sessions", "--no-formatting", check=False)
    if result.returncode != 0:
        return []
    return [
        line.strip().split()[0]
        for line in result.stdout.strip().splitlines()
        if line.strip() and "EXITED" not in line
    ]


def session_exists(session_name: str) -> bool:
    """Check if a Zellij session with the given name exists."""
    return session_name in list_sessions()


def delete_exited_session(session_name: str) -> bool:
    """Delete a Zellij session if it exists in EXITED state.

    Returns True if a session was deleted, False otherwise.
    """
    result = _run_zellij("list-sessions", "--no-formatting", check=False)
    if result.returncode != 0:
        return False
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split()
        if parts and parts[0] == session_name and "EXITED" in line:
            _run_zellij("delete-session", session_name, check=False)
            return True
    return False


def generate_layout(
    client: ClientConfig,
    panes: dict[str, dict[str, str]] | None = None,
    purposes: list[str] | None = None,
    *,
    session_mode: bool = True,
) -> Path:
    """Render the layout template for a client, returning the output path.

    Args:
        client: Client configuration.
        panes: Optional per-pane config. Each key is a pane name
               with a dict containing 'claude_cmd' and optionally 'cwd'.
        purposes: Ordered list of purpose names for panes. First is primary.
                  Defaults to client.auto_purposes values.
        session_mode: If True (default), include tab-bar and status-bar
                      plugins for initial session creation. If False,
                      emit only the tab content for ``new-tab`` injection.
    """
    GENERATED_LAYOUTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = GENERATED_LAYOUTS_DIR / f"cw-{client.name}.kdl"

    if purposes is None:
        purposes = [p.value for p in client.auto_purposes]

    # Default: plain claude with no session args
    if panes is None:
        panes = {name: {"claude_cmd": '"claude"'} for name in purposes}

    default_cwd = str(client.workspace_path)

    # Build pane context dicts for template
    def _pane_ctx(name: str) -> dict[str, str]:
        default_pane = {"claude_cmd": '"claude"'}
        pane_data = panes.get(name, default_pane) if panes else default_pane
        return {
            "name": name,
            "cwd": pane_data.get("cwd", default_cwd),
            "claude_cmd": pane_data.get("claude_cmd", '"claude"'),
        }

    primary_pane = _pane_ctx(purposes[0])
    secondary_panes = [_pane_ctx(p) for p in purposes[1:]]

    # Layout size rules based on pane count
    num_secondary = len(secondary_panes)
    if num_secondary == 0:
        primary_size = "100%"
        secondary_size = ""
    elif num_secondary == 1:
        primary_size = "50%"
        secondary_size = "50%"
    else:  # 2+
        primary_size = "50%"
        secondary_size = "50%"

    # Include cw-status plugin if WASM is installed
    cw_plugin_path = str(CW_PLUGIN_PATH) if CW_PLUGIN_PATH.exists() else None

    template = _env.get_template("client.kdl.j2")
    rendered = template.render(
        client_name=client.name,
        workspace_path=default_cwd,
        primary_pane=primary_pane,
        secondary_panes=secondary_panes,
        primary_size=primary_size,
        secondary_size=secondary_size,
        cw_plugin_path=cw_plugin_path,
        session_mode=session_mode,
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


def new_tab(
    client: ClientConfig,
    panes: dict[str, dict[str, str]] | None = None,
    purposes: list[str] | None = None,
    session: str | None = None,
) -> None:
    """Inject a new tab into a running Zellij session.

    Generates a tab-only layout (``session_mode=False``) and uses
    ``zellij action new-tab --layout`` to add it.
    """
    layout_path = generate_layout(
        client,
        panes=panes,
        purposes=purposes,
        session_mode=False,
    )
    base = ["-s", session] if session else []
    _run_zellij(*base, "action", "new-tab", "--layout", str(layout_path))


def rename_tab(
    new_name: str,
    session: str | None = None,
) -> None:
    """Rename the currently focused Zellij tab.

    Args:
        new_name: New tab name to display.
        session: Target a specific session by name.
                 If None, targets the current session.
    """
    base = ["-s", session] if session else []
    _run_zellij(*base, "action", "rename-tab", new_name, check=False)


def attach_session(session_name: str) -> None:
    """Attach to an existing Zellij session (takes over terminal)."""
    subprocess.run(["zellij", "attach", session_name], check=False)


def write_to_pane(text: str, session: str | None = None) -> None:
    """Write text to the currently focused Zellij pane.

    If *text* ends with ``\\n``, the newline is stripped and a raw Enter
    keypress (byte 13) is sent via ``zellij action write`` instead.
    This ensures applications like Claude Code that distinguish between
    a pasted newline and a real Enter keypress actually submit the input.

    Args:
        text: Text to inject as keystrokes.
        session: Target a specific session by name (for remote control).
                 If None, targets the current session (must be inside one).
    """
    send_enter = text.endswith("\n")
    if send_enter:
        text = text[:-1]

    base = ["-s", session] if session else []
    if text:
        _run_zellij(*base, "action", "write-chars", text)
    if send_enter:
        _run_zellij(*base, "action", "write", "13")


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


_RE_NAME = re.compile(r'name="([^"]+)"')
_RE_PANE_COMMAND = re.compile(r"pane\b.*\bcommand=")


def _iter_tab_pane_lines(
    session: str | None = None,
    tab_name: str | None = None,
) -> list[str]:
    """Return dump-layout lines belonging to a single tab.

    When *tab_name* is given, returns lines from the matching tab.
    Otherwise returns lines from the first tab (legacy behaviour).
    """
    base = ["-s", session] if session else []
    result = _run_zellij(*base, "action", "dump-layout", check=False)
    if result.returncode != 0:
        return []

    lines: list[str] = []
    in_target_tab = False
    for line in result.stdout.splitlines():
        if "tab " in line and "name=" in line:
            if in_target_tab:
                break  # Hit next tab, stop
            tab_match = _RE_NAME.search(line)
            current_tab = tab_match.group(1) if tab_match else None
            if tab_name is None or current_tab == tab_name:
                in_target_tab = True
            continue
        if in_target_tab:
            lines.append(line)
    return lines


def _pane_name_exists(
    pane_name: str,
    session: str | None = None,
    tab_name: str | None = None,
) -> bool:
    """Check whether a named pane exists in the layout."""
    for line in _iter_tab_pane_lines(session, tab_name):
        if _RE_PANE_COMMAND.search(line):
            match = _RE_NAME.search(line)
            if match and match.group(1) == pane_name:
                return True
    return False


def _get_focused_pane_name(
    session: str | None = None,
    tab_name: str | None = None,
) -> str | None:
    """Get the name of the currently focused pane from dump-layout.

    Looks for ``focus=true`` on pane lines (not tab lines) within
    the target tab.
    """
    for line in _iter_tab_pane_lines(session, tab_name):
        if "focus=true" in line and _RE_PANE_COMMAND.search(line):
            match = _RE_NAME.search(line)
            if match:
                return match.group(1)
    return None


# Max panes to cycle through before giving up
_MAX_PANE_CYCLE = 10


def focus_pane(
    pane_name: str,
    session: str | None = None,
    tab_name: str | None = None,
) -> None:
    """Focus a pane by name by cycling focus-next-pane.

    Uses dump-layout to check which pane name currently has focus,
    then cycles until the target pane is reached.

    When *tab_name* is given, only inspects that tab.
    """
    if not _pane_name_exists(pane_name, session, tab_name=tab_name):
        msg = f"Pane '{pane_name}' not found in layout."
        raise ZellijError(msg)

    current_name = _get_focused_pane_name(session, tab_name=tab_name)
    if current_name == pane_name:
        return  # Already there

    base = ["-s", session] if session else []
    for _ in range(_MAX_PANE_CYCLE):
        _run_zellij(*base, "action", "focus-next-pane", check=False)
        current_name = _get_focused_pane_name(session, tab_name=tab_name)
        if current_name == pane_name:
            return

    msg = f"Could not focus pane '{pane_name}' after cycling."
    raise ZellijError(msg)


def check_pane_health(
    session: str | None = None,
    tab_name: str | None = None,
) -> dict[str, bool]:
    """Check which named panes have running commands.

    Parses dump-layout to find panes with active command= processes.
    Returns a dict mapping pane name to whether its command is still running.

    When *tab_name* is given, only inspects the matching tab.
    Otherwise inspects the first tab (legacy behaviour).
    """
    health: dict[str, bool] = {}
    for line in _iter_tab_pane_lines(session, tab_name):
        name_match = _RE_NAME.search(line)
        if not name_match:
            continue
        pane_name = name_match.group(1)
        has_command = bool(re.search(r"\bcommand=", line))
        is_exited = "exited" in line.lower()
        health[pane_name] = has_command and not is_exited
    return health


def new_pane(
    command: str,
    *,
    name: str | None = None,
    cwd: str | None = None,
    direction: str = "down",
    close_on_exit: bool = True,
    session: str | None = None,
) -> None:
    """Open a new Zellij pane running a command.

    Args:
        command: Shell command to run in the new pane.
        name: Optional pane name.
        cwd: Working directory for the new pane.
        direction: Split direction (down, right, up, left).
        close_on_exit: Whether the pane closes when the command exits.
        session: Target a specific session by name (for remote control).
    """
    base = ["-s", session] if session else []
    args = [*base, "action", "new-pane", "--direction", direction]
    if name:
        args.extend(["--name", name])
    if cwd:
        args.extend(["--cwd", cwd])
    if close_on_exit:
        args.append("--close-on-exit")
    args.extend(["--", "bash", "-c", command])
    _run_zellij(*args)


def in_zellij_session() -> bool:
    """Check if we're currently running inside a Zellij session."""
    return "ZELLIJ_SESSION_NAME" in os.environ


def current_session_name() -> str | None:
    """Get the name of the current Zellij session, if inside one."""
    return os.environ.get("ZELLIJ_SESSION_NAME")


def resolve_session_target(default_session: str) -> str | None:
    """Return the Zellij session name to target, or None if already inside one.

    When running inside a Zellij session, actions target the current session
    implicitly (return None).  When outside, return ``default_session`` so
    callers can pass ``session=`` to Zellij action wrappers.
    """
    return None if in_zellij_session() else default_session
