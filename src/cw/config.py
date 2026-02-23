"""Configuration loading and state persistence."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import click
import yaml

from cw.exceptions import CwError
from cw.models import DEFAULT_AUTO_PURPOSES, ClientConfig, CwState, SessionPurpose

# Client names appear unquoted in shell commands (env var prefixes),
# filesystem paths (queue dirs, history dirs), and Zellij tab names.
# Restrict to safe characters to prevent injection.
_SAFE_CLIENT_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Branch names: alphanumeric, slashes, dots, dashes, underscores.
# Prevents YAML injection via crafted branch strings.
_SAFE_BRANCH_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$")

_xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
_xdg_data = os.environ.get("XDG_DATA_HOME", "")
CONFIG_DIR = (
    Path(_xdg_config) / "cw" if _xdg_config else Path.home() / ".config" / "cw"
)
STATE_DIR = (
    Path(_xdg_data) / "cw" if _xdg_data else Path.home() / ".local" / "share" / "cw"
)
QUEUES_DIR = STATE_DIR / "queues"
DAEMONS_DIR = STATE_DIR / "daemons"
EVENTS_DIR = STATE_DIR / "events"
HOOKS_DIR = STATE_DIR / "hooks"
HISTORY_DIR = STATE_DIR / "history"
CLIENTS_FILE = CONFIG_DIR / "clients.yaml"
STATE_FILE = STATE_DIR / "sessions.json"


def load_clients() -> dict[str, ClientConfig]:
    """Load client configurations from ~/.config/cw/clients.yaml."""
    if not CLIENTS_FILE.exists():
        return {}

    raw = yaml.safe_load(CLIENTS_FILE.read_text())
    if not raw or "clients" not in raw:
        return {}

    # Read global notification default
    global_notifications = bool(raw.get("notifications", False))

    clients: dict[str, ClientConfig] = {}
    for name, data in raw["clients"].items():
        if not _SAFE_CLIENT_NAME.match(name):
            msg = (
                f"Invalid client name '{name}':"
                " must match [a-zA-Z0-9][a-zA-Z0-9._-]*"
            )
            raise CwError(msg)
        client = ClientConfig(name=name, **data)
        # Apply global notification default if not set per-client
        if "notifications" not in data and global_notifications:
            client.notifications = True
        clients[name] = client
    return clients


def get_client(name: str) -> ClientConfig:
    """Get a client config by name, raising if not found."""
    clients = load_clients()
    if name not in clients:
        available = ", ".join(sorted(clients.keys())) or "(none configured)"
        msg = f"Unknown client '{name}'. Available: {available}"
        raise CwError(msg)
    return clients[name]


def detect_client_from_cwd() -> ClientConfig | None:
    """Try to detect the client from the current working directory.

    Skips worktree-mode clients whose ``workspace_path`` is a sentinel
    (equal to ``repo_path``) — their real path isn't known until start time.
    """
    cwd = Path.cwd()
    clients = load_clients()
    for client in clients.values():
        if client.is_worktree_client:
            continue
        try:
            cwd.relative_to(client.workspace_path)
            return client
        except ValueError:
            continue
    return None


def load_state() -> CwState:
    """Load persisted session state."""
    if not STATE_FILE.exists():
        return CwState()
    raw = json.loads(STATE_FILE.read_text())
    return CwState.model_validate(raw)


def save_state(state: CwState) -> None:
    """Persist session state to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(state.model_dump_json(indent=2))


def ensure_config() -> None:
    """Create config directory and example file if missing."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CLIENTS_FILE.exists():
        example = (
            Path(__file__).parent.parent.parent / "config" / "clients.example.yaml"
        )
        if example.exists():
            CLIENTS_FILE.write_text(example.read_text())
            click.echo(f"Created default config at {CLIENTS_FILE}")
        else:
            CLIENTS_FILE.write_text("clients: {}\n")
            click.echo(f"Created empty config at {CLIENTS_FILE}")


def show_config() -> None:
    """Display current configuration."""
    clients = load_clients()
    if not clients:
        click.echo("No clients configured.")
        click.echo(f"Edit {CLIENTS_FILE} to add clients.")
        return

    click.echo(f"Config: {CLIENTS_FILE}\n")
    for name, client in sorted(clients.items()):
        click.echo(f"  {name}:")
        if client.is_worktree_client:
            click.echo(f"    repo:   {client.repo_path}")
            click.echo(f"    branch: {client.branch}")
        else:
            click.echo(f"    path:   {client.workspace_path}")
            click.echo(f"    branch: {client.default_branch}")
        if client.auto_purposes != DEFAULT_AUTO_PURPOSES:
            purposes_str = ", ".join(p.value for p in client.auto_purposes)
            click.echo(f"    purposes: {purposes_str}")
        if client.worktree_base:
            click.echo(f"    worktrees: {client.worktree_base}")


def _is_git_repo(path: Path) -> bool:
    """Check if a path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


_VALID_PURPOSES = frozenset(p.value for p in SessionPurpose)


def _validate_purposes(purposes: list[str]) -> None:
    """Validate that all purpose strings are known SessionPurpose values."""
    for p in purposes:
        if p not in _VALID_PURPOSES:
            valid = ", ".join(sorted(_VALID_PURPOSES))
            msg = f"Invalid purpose '{p}'. Valid purposes: {valid}"
            raise CwError(msg)


def init_client(
    name: str,
    workspace_path: Path,
    *,
    default_branch: str = "main",
    auto_purposes: list[str] | None = None,
) -> None:
    """Add a new client to clients.yaml.

    Validates inputs, creates config dir/file if needed, and appends
    the new client as raw YAML text to preserve existing comments.
    """
    # Validate name
    if not _SAFE_CLIENT_NAME.match(name):
        msg = (
            f"Invalid client name '{name}':"
            " must start with alphanumeric and contain only [a-zA-Z0-9._-]"
        )
        raise CwError(msg)

    # Validate branch name (prevent YAML injection)
    if not _SAFE_BRANCH_NAME.match(default_branch):
        msg = (
            f"Invalid branch name '{default_branch}':"
            " must start with alphanumeric and contain only [a-zA-Z0-9/_.-]"
        )
        raise CwError(msg)

    # Validate purposes against known enum values
    if auto_purposes:
        _validate_purposes(auto_purposes)

    # Validate path exists
    if not workspace_path.is_dir():
        msg = f"Path does not exist or is not a directory: {workspace_path}"
        raise CwError(msg)

    # Validate it's a git repo
    if not _is_git_repo(workspace_path):
        msg = f"Path is not a git repository: {workspace_path}"
        raise CwError(msg)

    # Check for duplicate
    existing = load_clients()
    if name in existing:
        msg = f"Client '{name}' already exists in {CLIENTS_FILE}"
        raise CwError(msg)

    # Ensure config dir and file exist
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CLIENTS_FILE.exists():
        CLIENTS_FILE.write_text("clients:\n")
    else:
        # Verify file has a clients: key (guard against empty/malformed files)
        content = CLIENTS_FILE.read_text().strip()
        if not content:
            CLIENTS_FILE.write_text("clients:\n")
        elif "clients:" not in content:
            msg = (
                f"{CLIENTS_FILE} exists but has no 'clients:' key."
                " Add 'clients:' manually or delete the file to recreate."
            )
            raise CwError(msg)

    # Build YAML block for the new client (single-quote path for safety)
    escaped_path = str(workspace_path).replace("'", "''")
    lines = [
        f"\n  {name}:",
        f"    workspace_path: '{escaped_path}'",
        f"    default_branch: {default_branch}",
    ]
    if auto_purposes:
        purposes_str = ", ".join(auto_purposes)
        lines.append(f"    auto_purposes: [{purposes_str}]")

    block = "\n".join(lines) + "\n"

    # Append to file (preserves existing comments and formatting)
    with CLIENTS_FILE.open("a") as f:
        f.write(block)
