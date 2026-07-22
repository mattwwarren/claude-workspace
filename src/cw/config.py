"""Configuration loading and state persistence."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import yaml
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from cw import _config_migrate
from cw.atomic import atomic_write_text
from cw.exceptions import (
    ConfigValidationError,
    CwError,
    DispatchLoopLockedError,
    SessionsLockReentryError,
)
from cw.models import (
    CW_STATE_SCHEMA_VERSION,
    DEFAULT_AUTO_PURPOSES,
    ClientConfig,
    ConcurrencyOverrides,
    CwState,
    LaneConfig,
    OrchestratorConfig,
    SessionPurpose,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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
EVENTS_DIR = STATE_DIR / "events"
HISTORY_DIR = STATE_DIR / "history"
PR_WATCHER_DIR = STATE_DIR / "pr_watcher"
REVIEW_MONITOR_DIR = Path.home() / ".claude" / "review-monitor"
CLIENTS_FILE = CONFIG_DIR / "clients.yaml"
STATE_FILE = STATE_DIR / "sessions.json"

# Snapshot of the *real* (pre-monkeypatch) state/config roots, captured once
# at import time. refuse_real_state_write() compares write targets against
# these frozen values, so it still detects an escape even after the names
# above have been monkeypatched to a tmp_path by the autouse
# tmp_config_dir fixture (the ordinary, expected case) — see GitHub #1017.
_REAL_STATE_DIR = STATE_DIR
_REAL_CONFIG_DIR = CONFIG_DIR

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
# Process-lifetime singleton lock for the dispatch loop (#1362). A single
# GLOBAL file (no --client keying): only one run_dispatch_loop may run at a
# time against a given STATE_DIR.
DISPATCH_LOOP_LOCK = STATE_DIR / ".dispatch_loop.lock"

_DEFAULT_ORCHESTRATOR_YAML = """\
tick_interval_seconds: 30
default_ceiling: 2
per_client_ceiling: {}
# max_parallel_clients: null  # uncomment to cap how many clients dispatch per tick
linear_prefix_map: {}
reap_policy: signal_only  # default: signal only; set to auto to restore self-healing
# disallowed_mcp_tools: []  # patterns denied to every DAEMON worker, e.g.
#   ["mcp__plugin_linear_linear__*"] to block Linear MCP in headless workers.
#   MIGRATION: github-issues clients that relied on the old automatic Linear
#   block (#726) must set this explicitly now — the tracker heuristic is gone.
"""


# Path accessors — read module-level globals at call time so monkeypatching
# `cw.config.STATE_DIR` (etc.) reaches every consumer without needing to
# patch each module's own binding. Never `from cw.config import STATE_DIR`
# in a consumer; always call the accessor.


def config_dir() -> Path:
    return CONFIG_DIR


def state_dir() -> Path:
    return STATE_DIR


def events_dir() -> Path:
    return EVENTS_DIR


def history_dir() -> Path:
    return HISTORY_DIR


def pr_watcher_dir() -> Path:
    return PR_WATCHER_DIR


def diagnostics_dir(session_id: str) -> Path:
    """Return the per-session executor-diagnostics bundle dir (#1239).

    Executor-neutral: ``state_dir()/sessions/<session_id>/diagnostics``. Reads
    ``state_dir()`` at call time so monkeypatching ``cw.config.STATE_DIR``
    (the autouse ``tmp_config_dir`` fixture) reaches it, same as every other
    accessor here. Composition only — the directory is created lazily by the
    writer (``executor_diagnostics.persist_diagnostics_bundle``), not here.
    """
    return state_dir() / "sessions" / session_id / "diagnostics"


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


def dispatch_loop_lock_file() -> Path:
    return DISPATCH_LOOP_LOCK


def _under_pytest() -> bool:
    """Return True when running inside a pytest process. Monkeypatchable seam."""
    return "pytest" in sys.modules


def refuse_real_state_write(path: Path) -> None:
    """Raise CwError if *path* resolves under the real cw state/config dir
    while pytest is running.

    Belt-and-suspenders guard against GitHub #1017 (a live dev_queue.json
    clobbered by GEN-A/GEN-B test-fixture data): ``save_state``,
    ``save_usage_limited_until``, ``_save_concurrency_overrides``,
    ``save_dev_queue``, and ``init_client`` call this immediately before
    their atomic write, so a write that somehow escapes the autouse
    ``tmp_config_dir`` fixture (a stale module-level path binding, or a
    code path exercised before the fixture applies) fails loudly instead
    of silently corrupting ``~/.local/share/cw`` or ``~/.config/cw``. A
    no-op outside pytest — production ``cw`` runs are never affected.

    Detection is in-process only (``sys.modules`` membership): a real
    subprocess (e.g. a test that shells out to the installed ``cw``
    binary) spawns a fresh interpreter that never imports pytest, so this
    guard does not see or block writes from that process. See GitHub
    #1017's root-cause notes, which point at a subprocess/integration
    write path as a plausible cause of the original incident.
    """
    if not _under_pytest():
        return
    resolved = path.resolve()
    for real_root in (_REAL_STATE_DIR, _REAL_CONFIG_DIR):
        real_resolved = real_root.resolve()
        if resolved == real_resolved or real_resolved in resolved.parents:
            msg = (
                f"refusing real-state write: {path} resolves under "
                f"{real_root} while running under pytest (GitHub #1017 "
                "guard). Check for a stale module-level path binding or a "
                "write that runs before tmp_config_dir applies."
            )
            raise CwError(msg)


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


_sessions_lock_state = threading.local()


@contextlib.contextmanager
def sessions_lock() -> Iterator[None]:
    """Acquire an exclusive file lock over the sessions.json write window.

    Mirror of ``_queue_lock`` in ``cw.queue``. Hold this across every
    load_state → mutate → save_state sequence so concurrent ``cw``
    processes cannot clobber each other's mutations (last-writer-wins
    data loss). The lock is advisory (``fcntl.flock``) and per-open-fd,
    so sequential re-acquisitions in the same process (non-nested) are
    safe.

    Not reentrant: a same-thread nested acquisition raises
    :class:`~cw.exceptions.SessionsLockReentryError` instead of opening a
    second fd and deadlocking in ``flock()`` (GitHub #1228). The check is
    synchronous and runs before any file I/O or ``flock`` syscall, so it
    is hang-safe by construction. Callers that would otherwise re-enter
    (e.g. the RFC 0010 P4 review-recipe act phase calling back into
    ``reconcile()``) must already tolerate a ``CwError``-shaped failure on
    this path; see the callers of ``_dispatch_auto_fix_ci`` /
    ``_dispatch_address_review`` / ``_reconcile_usage_limited``.
    """
    if getattr(_sessions_lock_state, "held", False):
        msg = (
            "sessions_lock() re-entered on the same thread while already "
            "held; this would deadlock in flock() (GitHub #1228)"
        )
        raise SessionsLockReentryError(msg)
    state_dir().mkdir(parents=True, exist_ok=True)
    lock_path = sessions_lock_file()
    fd = lock_path.open("w")
    _sessions_lock_state.held = True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        _sessions_lock_state.held = False
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def mutate_state(fn: Callable[[CwState], None]) -> CwState:
    """Load state, apply fn in place under sessions_lock, save and return.

    Not reentrant: calling this while the caller already holds
    ``sessions_lock`` raises :class:`~cw.exceptions.SessionsLockReentryError`
    (GitHub #1228) instead of self-deadlocking. Code running inside a
    ``with sessions_lock():`` block (e.g. anything called from
    ``reconcile._reconcile_locked``) must mutate the loaded state and
    ``save_state`` directly instead.

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


def _current_command_str() -> str:
    """Return a normalized ``cw ...`` command string for the current process.

    Normalizes ``sys.argv[0]`` to its basename (dropping any interpreter/venv
    path) joined with the remaining argv via ``shlex.join`` -- e.g. ``cw
    dev-queue serve --quiet``. Written into the dispatch-loop lock file as the
    holder's self-reported identity (#1362).
    """
    argv0 = Path(sys.argv[0]).name if sys.argv else "cw"
    return shlex.join([argv0, *sys.argv[1:]])


def _dispatch_loop_holder_message(lock_path: Path) -> str:
    """Build the "already running" error message from the holder's identity JSON.

    Reads the pid+cmd JSON a live holder wrote into *lock_path*. Falls back to
    "holder unknown" text (rather than crashing) when the file is empty,
    unreadable, or malformed -- a losing acquisition attempt must surface a
    clean, actionable error even if the winner's identity is unavailable.
    """
    try:
        data = json.loads(lock_path.read_text())
        pid = data["pid"]
        cmd = data["cmd"]
    except (OSError, ValueError, KeyError, TypeError):
        return (
            "dispatch loop already running (holder unknown)"
            " — stop it first or use --force"
        )
    return (
        f"dispatch loop already running (pid {pid}: {cmd})"
        " — stop it first or use --force"
    )


@contextlib.contextmanager
def dispatch_loop_lock() -> Iterator[None]:
    """Acquire the process-lifetime singleton lock for the dispatch loop (#1362).

    Only one :func:`cw.dispatch.run_dispatch_loop` may run at a time against a
    given ``STATE_DIR``. Unlike the sibling ``*_lock`` context managers here
    (which block on ``LOCK_EX``), this acquires ``LOCK_EX | LOCK_NB`` so a
    second launch fails fast with :class:`~cw.exceptions.DispatchLoopLockedError`
    instead of blocking behind the running loop. The advisory lock is held for
    the full lifetime of the ``with`` block and released on every exit path
    (normal return, ``once=True`` early return, any raised exception).

    Divergence from the sibling skeleton: the file is opened ``"r+"`` after
    ``touch(exist_ok=True)`` rather than ``"w"``. ``"w"`` truncates on open
    *before* the flock attempt, so a losing non-blocking acquisition would
    destroy the winning holder's still-live identity JSON. On a successful
    acquisition the file is truncated and re-populated with this process's
    ``{"pid", "cmd"}`` identity so a later contender can name the holder.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    lock_path = dispatch_loop_lock_file()
    lock_path.touch(exist_ok=True)
    fd = lock_path.open("r+")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            message = _dispatch_loop_holder_message(lock_path)
            raise DispatchLoopLockedError(message) from exc
        # Won the lock — safe to truncate and write our identity now.
        #
        # Why not an atomic temp-file + os.replace() here: os.replace() would
        # swap the underlying inode out from under this held fd's flock,
        # leaving a fresh, unlocked inode at this path that a losing
        # contender could then successfully flock -- breaking the mutex
        # itself, which is strictly worse than the narrow race this would
        # fix. There IS a real (but narrow) window between this truncate and
        # the write below completing where a concurrent losing acquirer's
        # unlocked read (_dispatch_loop_holder_message) could see stale or
        # empty content; a fully correct fix requires a separate, atomically
        # -replaced holder-info file independent of the lock file, which is
        # more design than this narrow, cosmetic-only race (worst case: a
        # transiently stale-but-plausible PID in an error message that
        # already degrades gracefully to "holder unknown" on any read
        # failure) currently warrants.
        fd.seek(0)
        fd.truncate()
        fd.write(json.dumps({"pid": os.getpid(), "cmd": _current_command_str()}))
        fd.flush()
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
        try:
            client = ClientConfig(name=name, **data)
        except ValidationError as exc:
            msg = f"{path}: invalid config for client '{name}': {exc}"
            raise ConfigValidationError(msg) from exc
        # Apply global notification default if not set per-client
        if "notifications" not in data and global_notifications:
            client.notifications = True
        clients[name] = client
    return clients


def _lookup_client(clients: dict[str, ClientConfig], name: str) -> ClientConfig:
    """Look up name in clients, raising CwError with an available-clients hint."""
    if name not in clients:
        available = ", ".join(sorted(clients.keys())) or "(none configured)"
        msg = f"Unknown client '{name}'. Available: {available}"
        raise CwError(msg)
    return clients[name]


def get_client(name: str) -> ClientConfig:
    """Get a client config by name, raising if not found."""
    return _lookup_client(load_clients(), name)


def load_state() -> CwState:
    """Load persisted session state, applying schema migrations."""
    path = state_file()
    if not path.exists():
        return CwState()
    raw = json.loads(path.read_text())
    _backup_state_file(raw)
    return CwState.model_validate(_config_migrate.migrate_cw_state(raw))


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
    refuse_real_state_write(state_file())
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
    try:
        return OrchestratorConfig.model_validate(raw)
    except ValidationError as exc:
        msg = f"{path}: {exc}"
        raise ConfigValidationError(msg) from exc


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
    refuse_real_state_write(path)
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

    # Lane-level overrides (paused, max_parallel) are per-ClientConfig, not
    # per-OrchestratorConfig.  They are applied by load_effective_clients().

    if updates:
        return declared.model_copy(update=updates)
    return declared


def load_effective_clients() -> dict[str, ClientConfig]:
    """Return clients with lane-level runtime overrides (pause, max_parallel) merged in.

    Reads declared clients from clients.yaml and applies lane-level overrides
    from concurrency_overrides.json.  Override wins when not None.  Returns new
    ClientConfig objects; does not mutate clients.yaml on disk.

    Use this instead of load_clients() wherever the scheduler makes per-lane
    dispatch decisions so that cw lane pause/resume affects running dispatches.
    """
    clients = load_clients()
    overrides = _load_concurrency_overrides()
    if not overrides.lanes:
        return clients
    result: dict[str, ClientConfig] = {}
    for name, client in clients.items():
        patched: list[LaneConfig] = []
        any_changed = False
        for lane_cfg in client.effective_lanes:
            key = f"{name}/{lane_cfg.name}"
            lane_override = overrides.lanes.get(key)
            if lane_override is not None and lane_override.paused is not None:
                patched.append(
                    lane_cfg.model_copy(update={"paused": lane_override.paused})
                )
                any_changed = True
            else:
                patched.append(lane_cfg)
        if any_changed:
            result[name] = client.model_copy(update={"lanes": patched})
        else:
            result[name] = client
    return result


def get_effective_client(name: str) -> ClientConfig:
    """Get a client config with lane-level runtime overrides merged, raising if absent.

    Effective analogue of :func:`get_client`: shares its lookup/raise contract
    (via :func:`_lookup_client`) over :func:`load_effective_clients` so callers
    that surface lane pause/circuit-breaker state (``cw lane ls``) reflect
    overrides. See #875.
    """
    return _lookup_client(load_effective_clients(), name)


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
        # Why: guarded here (immediately before the content write) rather than
        # before config_dir().mkdir()/clients_lock() above, unlike the other
        # refuse_real_state_write call sites. A pre-guard escape only costs an
        # idempotent mkdir(exist_ok=True) and a lock-file touch against the
        # real config dir — not the data-clobbering content write this guard
        # exists to prevent — so closing the lock-acquisition window wasn't
        # judged worth the larger, differently-shaped change of guarding
        # every *_lock() context manager. See GitHub #1017 plan decision.
        refuse_real_state_write(clients_path)
        atomic_write_text(clients_path, buf.getvalue())
