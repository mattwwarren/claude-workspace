"""Configuration loading and state persistence."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import click
import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from cw.atomic import atomic_write_text
from cw.exceptions import CwError
from cw.models import (
    CW_STATE_SCHEMA_VERSION,
    DEFAULT_AUTO_PURPOSES,
    ClientConfig,
    CwState,
    OrchestratorConfig,
    SessionOrigin,
    SessionPurpose,
)

logger = logging.getLogger(__name__)

# Client names appear unquoted in shell commands (env var prefixes),
# filesystem paths (queue dirs, history dirs), and multiplexer workspace
# names. Restrict to safe characters to prevent injection.
_SAFE_CLIENT_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Branch names: alphanumeric, slashes, dots, dashes, underscores.
# Prevents YAML injection via crafted branch strings.
_SAFE_BRANCH_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$")

_EMPTY_CLIENTS_DOC = "clients:\n"

_xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
_xdg_data = os.environ.get("XDG_DATA_HOME", "")
CONFIG_DIR = Path(_xdg_config) / "cw" if _xdg_config else Path.home() / ".config" / "cw"
STATE_DIR = (
    Path(_xdg_data) / "cw" if _xdg_data else Path.home() / ".local" / "share" / "cw"
)
QUEUES_DIR = STATE_DIR / "queues"
EVENTS_DIR = STATE_DIR / "events"
HISTORY_DIR = STATE_DIR / "history"
PR_WATCHER_DIR = STATE_DIR / "pr_watcher"
REVIEW_MONITOR_DIR = Path.home() / ".claude" / "review-monitor"
CLIENTS_FILE = CONFIG_DIR / "clients.yaml"
STATE_FILE = STATE_DIR / "sessions.json"

ORCHESTRATOR_CONFIG_DIR = Path.home() / ".claude-workspace"
ORCHESTRATOR_CONFIG_FILE = ORCHESTRATOR_CONFIG_DIR / "orchestrator.yaml"
DEV_QUEUE_FILE = STATE_DIR / "dev_queue.json"
DEV_QUEUE_LOCK = STATE_DIR / ".dev_queue.lock"
DEV_PLAN_FILE = STATE_DIR / "dev_plan.json"
DEV_PLAN_LOCK = STATE_DIR / ".dev_plan.lock"
DEV_PLAN_OUTPUT_DIR = STATE_DIR / "plan_output"

_DEFAULT_ORCHESTRATOR_YAML = """\
tick_interval_seconds: 30
per_client_max_parallel:
  default: 2
linear_prefix_map: {}
"""


# Path accessors — read module-level globals at call time so monkeypatching
# `cw.config.STATE_DIR` (etc.) reaches every consumer without needing to
# patch each module's own binding. Never `from cw.config import STATE_DIR`
# in a consumer; always call the accessor.


def config_dir() -> Path:
    return CONFIG_DIR


def state_dir() -> Path:
    return STATE_DIR


def queues_dir() -> Path:
    return QUEUES_DIR


def events_dir() -> Path:
    return EVENTS_DIR


def history_dir() -> Path:
    return HISTORY_DIR


def pr_watcher_dir() -> Path:
    return PR_WATCHER_DIR


def review_monitor_dir() -> Path:
    return REVIEW_MONITOR_DIR


def clients_file() -> Path:
    return CLIENTS_FILE


def state_file() -> Path:
    return STATE_FILE


def orchestrator_config_file() -> Path:
    return ORCHESTRATOR_CONFIG_FILE


def dev_queue_file() -> Path:
    return DEV_QUEUE_FILE


def dev_queue_lock() -> Path:
    return DEV_QUEUE_LOCK


def dev_plan_file() -> Path:
    return DEV_PLAN_FILE


def dev_plan_lock() -> Path:
    return DEV_PLAN_LOCK


def dev_plan_output_dir() -> Path:
    return DEV_PLAN_OUTPUT_DIR


def load_clients() -> dict[str, ClientConfig]:
    """Load client configurations from ~/.config/cw/clients.yaml."""
    path = clients_file()
    if not path.exists():
        return {}

    raw = yaml.safe_load(path.read_text())
    if not raw or "clients" not in raw:
        return {}

    # Read global notification default
    global_notifications = bool(raw.get("notifications", False))

    clients: dict[str, ClientConfig] = {}
    for name, data in raw["clients"].items():
        if not _SAFE_CLIENT_NAME.match(name):
            msg = (
                f"Invalid client name '{name}':"
                " must start with alphanumeric and contain only [a-zA-Z0-9._-]"
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


def load_state() -> CwState:
    """Load persisted session state, applying schema migrations."""
    path = state_file()
    if not path.exists():
        return CwState()
    raw = json.loads(path.read_text())
    return CwState.model_validate(migrate_cw_state(raw))


_VALID_SESSION_ORIGINS = frozenset(v.value for v in SessionOrigin)


def migrate_cw_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw sessions.json payload into a currently-valid shape.

    The goal is to never brick the tool on a state file that was written by
    an older (or briefly-diverged) version of cw. Unknown or renamed fields
    are coerced; unknown enum values are reset to a safe default with a
    warning rather than raising a validation error.
    """
    sessions = raw.get("sessions")
    if isinstance(sessions, list):
        for session_raw in sessions:
            if not isinstance(session_raw, dict):
                continue
            _migrate_zellij_fields(session_raw)
            _coerce_session_origin(session_raw)
            _migrate_v1_linkage_fields(session_raw)
    # Bump persisted schema_version to current after all migration steps.
    raw["schema_version"] = CW_STATE_SCHEMA_VERSION
    return raw


def _migrate_zellij_fields(session_raw: dict[str, Any]) -> None:
    """Rename the pre-0.4 zellij_pane field and drop zellij_tab.

    Migration armor — do not delete. Users in the wild still have
    `sessions.json` files from the Zellij era; the rename runs every load
    so upgrades stay transparent.
    """
    if "zellij_pane" in session_raw and "surface_ref" not in session_raw:
        session_raw["surface_ref"] = session_raw.pop("zellij_pane")
    else:
        session_raw.pop("zellij_pane", None)
    session_raw.pop("zellij_tab", None)


def _coerce_session_origin(session_raw: dict[str, Any]) -> None:
    """Reset unknown SessionOrigin values to 'user' with a warning.

    A stale sessions.json containing, for example, `origin: "delegate"`
    (a value that briefly existed in a branch but never landed) used to
    crash every cw command at Pydantic validation. Coerce instead, so
    users aren't locked out of their own state.
    """
    origin = session_raw.get("origin")
    if origin is not None and origin not in _VALID_SESSION_ORIGINS:
        logger.warning(
            "session %s has unknown origin %r; coercing to 'user'",
            session_raw.get("id", "<unknown>"),
            origin,
        )
        session_raw["origin"] = SessionOrigin.USER.value


def _migrate_v1_linkage_fields(session_raw: dict[str, Any]) -> None:
    """Fill parent_session_id and worker_session_ids introduced in schema v2.

    Idempotent: if the fields are already present they are left untouched,
    so a v2 file round-trips without modification.
    """
    if "parent_session_id" not in session_raw:
        session_raw["parent_session_id"] = None
    if "worker_session_ids" not in session_raw:
        session_raw["worker_session_ids"] = []


def save_state(state: CwState) -> None:
    """Persist session state to disk atomically."""
    state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_file(), state.model_dump_json(indent=2))


def load_orchestrator_config() -> OrchestratorConfig:
    """Load orchestrator.yaml, creating with defaults if missing."""
    path = orchestrator_config_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_ORCHESTRATOR_YAML)
    raw = yaml.safe_load(path.read_text())
    if not raw:
        return OrchestratorConfig()
    return OrchestratorConfig.model_validate(raw)


def ensure_config() -> None:
    """Create config directory and example file if missing."""
    config_dir().mkdir(parents=True, exist_ok=True)
    clients_path = clients_file()
    if not clients_path.exists():
        example = (
            Path(__file__).parent.parent.parent / "config" / "clients.example.yaml"
        )
        if example.exists():
            clients_path.write_text(example.read_text())
            click.echo(f"Created default config at {clients_path}")
        else:
            clients_path.write_text("clients: {}\n")
            click.echo(f"Created empty config at {clients_path}")


def show_config() -> None:
    """Display current configuration."""
    clients = load_clients()
    clients_path = clients_file()
    if not clients:
        click.echo("No clients configured.")
        click.echo(f"Edit {clients_path} to add clients.")
        return

    click.echo(f"Config: {clients_path}\n")
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
        # Strip GIT_* env vars so leaked worktree env doesn't affect detection.
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
            env=clean_env,
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


def _validate_init_inputs(
    name: str,
    workspace_path: Path,
    default_branch: str,
    auto_purposes: list[str] | None,
) -> None:
    """Validate all inputs for init_client.

    Performs format validation (name, branch, purposes) and filesystem
    checks (directory exists, git repository). The git check invokes
    a subprocess.
    """
    if not _SAFE_CLIENT_NAME.match(name):
        msg = (
            f"Invalid client name '{name}':"
            " must start with alphanumeric and contain only [a-zA-Z0-9._-]"
        )
        raise CwError(msg)

    if not _SAFE_BRANCH_NAME.match(default_branch):
        msg = (
            f"Invalid branch name '{default_branch}':"
            " must start with alphanumeric and contain only [a-zA-Z0-9/_.-]"
        )
        raise CwError(msg)

    if auto_purposes:
        _validate_purposes(auto_purposes)

    if not workspace_path.is_dir():
        msg = f"Path does not exist or is not a directory: {workspace_path}"
        raise CwError(msg)

    if not _is_git_repo(workspace_path):
        msg = f"Path is not a git repository: {workspace_path}"
        raise CwError(msg)


def init_client(
    name: str,
    workspace_path: Path,
    *,
    default_branch: str = "main",
    auto_purposes: list[str] | None = None,
) -> None:
    """Add a new client to clients.yaml.

    Validates inputs, creates config dir/file if needed, and uses
    ruamel.yaml round-trip parsing to preserve existing comments.
    """
    _validate_init_inputs(name, workspace_path, default_branch, auto_purposes)

    # Ensure config dir exists
    config_dir().mkdir(parents=True, exist_ok=True)
    clients_path = clients_file()

    # Round-trip parse-modify-write with ruamel.yaml (preserves comments)
    rt = YAML(typ="rt")
    rt.default_flow_style = False

    if clients_path.exists():
        content = clients_path.read_text()
        doc = rt.load(content) if content.strip() else rt.load(_EMPTY_CLIENTS_DOC)
    else:
        doc = rt.load(_EMPTY_CLIENTS_DOC)

    if not isinstance(doc, dict) or "clients" not in doc:
        msg = (
            f"{clients_path} exists but has no 'clients:' key."
            " Add 'clients:' manually or delete the file to recreate."
        )
        raise CwError(msg)

    clients_map = doc["clients"]
    if clients_map is None:
        clients_map = CommentedMap()
        doc["clients"] = clients_map

    if name in clients_map:
        msg = f"Client '{name}' already exists in {clients_path}"
        raise CwError(msg)

    # Build the new client entry
    entry = CommentedMap()
    entry["workspace_path"] = str(workspace_path)
    entry["default_branch"] = default_branch
    if auto_purposes:
        entry["auto_purposes"] = auto_purposes

    clients_map[name] = entry

    with clients_path.open("w") as f:
        rt.dump(doc, f)
