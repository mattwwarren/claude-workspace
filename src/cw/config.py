"""Configuration loading and state persistence."""

from __future__ import annotations

import json
from pathlib import Path

import click
import yaml

from cw.models import ClientConfig, CwState

CONFIG_DIR = Path.home() / ".config" / "cw"
STATE_DIR = Path.home() / ".local" / "share" / "cw"
CLIENTS_FILE = CONFIG_DIR / "clients.yaml"
STATE_FILE = STATE_DIR / "sessions.json"


def load_clients() -> dict[str, ClientConfig]:
    """Load client configurations from ~/.config/cw/clients.yaml."""
    if not CLIENTS_FILE.exists():
        return {}

    raw = yaml.safe_load(CLIENTS_FILE.read_text())
    if not raw or "clients" not in raw:
        return {}

    clients: dict[str, ClientConfig] = {}
    for name, data in raw["clients"].items():
        clients[name] = ClientConfig(name=name, **data)
    return clients


def get_client(name: str) -> ClientConfig:
    """Get a client config by name, raising if not found."""
    clients = load_clients()
    if name not in clients:
        available = ", ".join(sorted(clients.keys())) or "(none configured)"
        raise click.ClickException(f"Unknown client '{name}'. Available: {available}")
    return clients[name]


def detect_client_from_cwd() -> ClientConfig | None:
    """Try to detect the client from the current working directory."""
    cwd = Path.cwd()
    clients = load_clients()
    for client in clients.values():
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
        example = Path(__file__).parent.parent.parent / "config" / "clients.example.yaml"
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
        click.echo(f"    path:   {client.workspace_path}")
        click.echo(f"    branch: {client.default_branch}")
        if client.worktree_base:
            click.echo(f"    worktrees: {client.worktree_base}")
