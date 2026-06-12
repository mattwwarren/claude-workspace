"""Configuration loading and state persistence."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    ConcurrencyOverrides,
    CwState,
    OrchestratorConfig,
    SessionOrigin,
    SessionPurpose,
)
from cw.native_daemon import SHORT_SESSION_ID_RE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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
SESSIONS_LOCK = STATE_DIR / ".sessions.lock"
CLIENTS_LOCK = CONFIG_DIR / ".clients.yaml.lock"
CONCURRENCY_OVERRIDE_FILE = STATE_DIR / "concurrency_overrides.json"
CONCURRENCY_OVERRIDE_LOCK = STATE_DIR / ".concurrency_overrides.lock"

_DEFAULT_ORCHESTRATOR_YAML = """\
tick_interval_seconds: 30
default_ceiling: 2
per_client_ceiling: {}
# max_parallel_clients: null  # uncomment to cap how many clients dispatch per tick
linear_prefix_map: {}
reap_policy: signal_only  # default: signal only; set to auto to restore self-healing
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


def sessions_lock_file() -> Path:
    return SESSIONS_LOCK


def clients_lock_file() -> Path:
    return CLIENTS_LOCK


def concurrency_override_file() -> Path:
    return CONCURRENCY_OVERRIDE_FILE


def concurrency_override_lock_file() -> Path:
    return CONCURRENCY_OVERRIDE_LOCK


@contextlib.contextmanager
def concurrency_override_lock() -> Iterator[None]:
    """Acquire an exclusive file lock over the concurrency_overrides.json write window.

    Mirror of ``sessions_lock``. Hold this across every load→mutate→write
    sequence for concurrency overrides so concurrent processes cannot clobber
    each other's edits. The lock is advisory (``fcntl.flock``) and per-open-fd,
    so sequential re-acquisitions in the same process are safe.
    Do NOT nest: acquiring while already holding will deadlock.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    lock_path = concurrency_override_lock_file()
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


@contextlib.contextmanager
def sessions_lock() -> Iterator[None]:
    """Acquire an exclusive file lock over the sessions.json write window.

    Mirror of ``_queue_lock`` in ``cw.queue``. Hold this across every
    load_state → mutate → save_state sequence so concurrent ``cw``
    processes cannot clobber each other's mutations (last-writer-wins
    data loss). The lock is advisory (``fcntl.flock``) and per-open-fd,
    so sequential re-acquisitions in the same process (non-nested) are
    safe. Do NOT nest: acquiring while already holding will deadlock.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    lock_path = sessions_lock_file()
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def mutate_state(fn: Callable[[CwState], None]) -> CwState:
    """Load state, apply fn in place under sessions_lock, save and return.

    Not reentrant: ``sessions_lock`` is a per-open-fd ``flock``, so calling
    this while the caller already holds ``sessions_lock`` self-deadlocks.
    Code running inside a ``with sessions_lock():`` block (e.g. anything
    called from ``reconcile._reconcile_locked``) must mutate the loaded
    state and ``save_state`` directly instead.

    Invariant: every ``save_state`` call outside ``config.py`` is either
    inside a ``with sessions_lock():`` block **or** in a helper whose
    docstring states the caller must hold the lock (the reconcile pattern).
    Sites in ``cli.py``, ``session.py``, and ``orchestrate.py`` that cannot
    use this helper carry a ``# Why not mutate_state:`` comment explaining
    the disqualifying condition (subprocess inside lock, dual-lock, or
    network call).
    """
    with sessions_lock():
        state = load_state()
        fn(state)
        save_state(state)
        return state


@contextlib.contextmanager
def clients_lock() -> Iterator[None]:
    """Acquire an exclusive file lock over the clients.yaml write window.

    Mirror of ``sessions_lock``. Hold this across every load→mutate→write
    sequence in ``init_client`` (and any future client-mutating command) so
    concurrent ``cw client add`` processes cannot clobber each other's edits
    (last-writer-wins data loss). The lock is advisory (``fcntl.flock``) and
    per-open-fd, so sequential re-acquisitions in the same process are safe.
    Do NOT nest: acquiring while already holding will deadlock.
    """
    config_dir().mkdir(parents=True, exist_ok=True)
    lock_path = clients_lock_file()
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


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
    _backup_state_file(raw)
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
    if "sessions" in raw and not isinstance(sessions, list):
        # Malformed payload — leave schema_version untouched so the
        # corruption surfaces downstream rather than getting a false
        # "fully migrated" stamp.
        return raw
    # Capture the on-disk version before we bump it so per-step guards
    # can condition on "is this an upgrade from version X?".
    on_disk_version = int(raw.get("schema_version") or 0)
    if isinstance(sessions, list):
        for session_raw in sessions:
            if not isinstance(session_raw, dict):
                continue
            _migrate_zellij_fields(session_raw)
            # Only clear legacy multiplexer surface_refs during the v4→v5
            # upgrade pass.  After migration the field may legally hold any
            # string set by the live daemon path; re-clearing it on every
            # load would wipe valid programmatic writes (e.g. test fixtures,
            # daemon-spawn short ids that happen to look like plain strings).
            if on_disk_version < 5:
                _clear_non_hex_surface_refs(session_raw)
            _coerce_session_origin(session_raw)
            _fill_linkage_field_defaults(session_raw)
            _fill_last_result_default(session_raw)
            _fill_cost_fields_default(session_raw)
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


def _fill_linkage_field_defaults(session_raw: dict[str, Any]) -> None:
    """Fill parent_session_id and worker_session_ids introduced in schema v2.

    Runs unconditionally and is idempotent: if the fields are already present
    they are left untouched, so a v2 file round-trips without modification.
    The canonical source of truth for these defaults is the Session Pydantic
    model; this helper exists only to ensure the on-disk file gets the keys
    explicitly so re-saves don't lose them.
    """
    if "parent_session_id" not in session_raw:
        session_raw["parent_session_id"] = None
    if "worker_session_ids" not in session_raw:
        session_raw["worker_session_ids"] = []


def _fill_last_result_default(session_raw: dict[str, Any]) -> None:
    """Fill last_result introduced in schema v3.

    Idempotent like the linkage defaults helper. Sessions that pre-date the
    headless auto-dev parser have no last_result on disk; setting None
    explicitly keeps the on-disk shape stable across re-saves.
    """
    if "last_result" not in session_raw:
        session_raw["last_result"] = None


def _fill_cost_fields_default(session_raw: dict[str, Any]) -> None:
    """Fill cost_usd and cost_breakdown introduced in schema v4.

    Idempotent: existing values are preserved.
    """
    if "cost_usd" not in session_raw:
        session_raw["cost_usd"] = None
    if "cost_breakdown" not in session_raw:
        session_raw["cost_breakdown"] = None


def _clear_non_hex_surface_refs(session_raw: dict[str, Any]) -> None:
    """Clear non-native surface_ref values (legacy cmux/tmux pane IDs).

    Native daemon workers store an 8-char hex short-id as surface_ref.
    Legacy cmux/tmux backends stored pane references like "ws:0.1" or
    "tmux-pane-3". Clear any value that doesn't match the native hex
    pattern so stale references don't confuse reconcile.
    """
    surface_ref = session_raw.get("surface_ref")
    if surface_ref is None:
        return
    if not SHORT_SESSION_ID_RE.fullmatch(surface_ref):
        session_raw["surface_ref"] = None


def _backup_state_file(raw: dict[str, Any]) -> None:
    """Back up sessions.json before the first v5 migration. Idempotent.

    Only runs when the on-disk schema_version is below the current version
    AND the backup doesn't already exist. This preserves the pre-migration
    state for manual recovery.
    """
    path = state_file()
    if not path.exists():
        return
    backup = path.parent / f".{path.name}.0.x-backup"
    if backup.exists():
        return
    if int(raw.get("schema_version") or 0) >= CW_STATE_SCHEMA_VERSION:
        return
    shutil.copy2(path, backup)


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


def _load_concurrency_overrides() -> ConcurrencyOverrides:
    """Load ConcurrencyOverrides from disk; return empty model if absent or invalid."""
    path = concurrency_override_file()
    if not path.exists():
        return ConcurrencyOverrides()
    try:
        return ConcurrencyOverrides.model_validate_json(path.read_text())
    except (ValueError, OSError):
        return ConcurrencyOverrides()


def _save_concurrency_overrides(overrides: ConcurrencyOverrides) -> None:
    """Persist ConcurrencyOverrides to disk atomically (caller holds lock)."""
    path = concurrency_override_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, overrides.model_dump_json())


def load_effective_config() -> OrchestratorConfig:
    """Return OrchestratorConfig with runtime overrides merged in.

    Loads declared config from orchestrator.yaml and runtime overrides from
    concurrency_overrides.json.  Override values win over declared values when
    not None.  Returns a new OrchestratorConfig; does not mutate the declared
    config on disk.
    """
    declared = load_orchestrator_config()
    overrides = _load_concurrency_overrides()

    updates: dict[str, object] = {}

    if overrides.max_parallel_clients is not None:
        updates["max_parallel_clients"] = overrides.max_parallel_clients

    if overrides.clients:
        merged_ceiling = dict(declared.per_client_ceiling)
        for client_name, client_override in overrides.clients.items():
            if client_override.ceiling is not None:
                merged_ceiling[client_name] = client_override.ceiling
        updates["per_client_ceiling"] = merged_ceiling

    if overrides.lanes:
        # Build the list of LaneConfig objects for the effective config.
        # Lane overrides here apply globally (by "client/lane" key) — the
        # dispatcher reads effective_lanes from ClientConfig, so we store
        # lane paused/max_parallel overrides as a side-channel that dispatch
        # can consult.  For now we surface them via OrchestratorConfig
        # so load_effective_config returns a single unified config.
        pass  # Lane-level overrides are consumed directly by the CLI/dispatch

    if updates:
        return declared.model_copy(update=updates)
    return declared


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
    except OSError:
        return False
    else:
        return result.returncode == 0


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

    with clients_lock():
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

        buf = StringIO()
        rt.dump(doc, buf)
        atomic_write_text(clients_path, buf.getvalue())
