"""In-process reader for a non-Claude backend's MCP server declaration (#3055).

RFC 0018 S3's loud-failure floor needs one fact before an opencode PLAN/IMPL
row spawns, and ``cw doctor`` needs the same fact per lane: **does the
backend's own config declare the tracker's MCP server?** This module answers
it from the user-layer config file alone, parsed in process (``tomllib`` for
codex, strict ``json`` for opencode). It never shells out, so it is safe on
the dispatch tick.

Contracts:

* **Positive evidence only.** ``ABSENT`` and ``DISABLED`` come only from a
  user-layer file that parsed cleanly and either lacks the server entry or
  sets ``enabled = false`` on it. A missing file, an ``OSError``, a parse
  error (including JSONC comments in ``opencode.json``), a wrong container
  type or a non-bool ``enabled`` is ``UNKNOWN`` with a reason: callers fail
  open on it.
* **Other layers downgrade, never upgrade.** Codex and opencode merge project
  and system layers over the user file. Those are not parsed here; when one
  exists (``Path.exists()`` only, never opened), an ``ABSENT``/``DISABLED``
  verdict becomes ``UNKNOWN`` because the server may be declared there. An
  ``ENABLED`` verdict is never downgraded. A false ``UNKNOWN`` costs a
  warning; a false ``ABSENT`` would park a healthy row.
* **Declaration only.** A declared server says nothing about auth, env or
  whether the headless session loads it; ``cw doctor`` says so verbatim.

Known limits: the probe walks the client root, not the ticket worktree's
committed files, and codex ``--profile`` files and ``-c`` overrides are not
probed.

Leaf module: standard library only, so dispatch and doctor can both import it.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

# Backend names, equal to ``cw.models.orchestrator_config.CODEX_BACKEND`` and
# ``OPENCODE_BACKEND`` (pinned by tests/test_backend_mcp_config.py). Literal
# here because this module imports the standard library only.
_CODEX = "codex"
_OPENCODE = "opencode"

_CODEX_HOME_ENV = "CODEX_HOME"
_CODEX_SERVERS_KEY = "mcp_servers"
_OPENCODE_SERVERS_KEY = "mcp"
_ENABLED_KEY = "enabled"
# The repo-root marker the project-layer walk stops at.
_GIT_MARKER = ".git"

# Env vars that point opencode at a config the user-layer read cannot see.
# A conservative superset: any one being set downgrades a negative verdict.
_OPENCODE_ENV_VARS: tuple[str, ...] = (
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
)

_OPENCODE_PROJECT_NAMES: tuple[Path, ...] = (
    Path("opencode.json"),
    Path("opencode.jsonc"),
    Path(".opencode", "opencode.json"),
    Path(".opencode", "opencode.jsonc"),
)
_CODEX_PROJECT_NAMES: tuple[Path, ...] = (Path(".codex", "config.toml"),)

_PROJECT_LAYER_NAMES: dict[str, tuple[Path, ...]] = {
    _CODEX: _CODEX_PROJECT_NAMES,
    _OPENCODE: _OPENCODE_PROJECT_NAMES,
}

# System-wide layers, probed for existence only. A module constant so the test
# fixture ``isolated_backend_layers`` can empty it; a developer's real
# /etc/opencode or /etc/codex must never leak into a test.
_SYSTEM_LAYERS: Mapping[str, tuple[Path, ...]] = {
    _CODEX: (Path("/etc/codex/config.toml"),),
    _OPENCODE: (
        Path("/etc/opencode/opencode.json"),
        Path("/etc/opencode/opencode.jsonc"),
        Path("/Library/Application Support/opencode/opencode.json"),
        Path("/Library/Application Support/opencode/opencode.jsonc"),
    ),
}


class McpState(StrEnum):
    """What the user-layer config says about one MCP server."""

    ENABLED = "enabled"
    ABSENT = "absent"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class McpConfigReading:
    """One reading: the state, the user-layer path it came from, and why."""

    state: McpState
    path: Path
    reason: str


def user_layer_path(backend: str) -> Path | None:
    """The user-layer config path for *backend*, or ``None`` if unsupported."""
    if backend == _CODEX:
        codex_home = os.environ.get(_CODEX_HOME_ENV)
        base = Path(codex_home) if codex_home else Path.home() / ".codex"
        return base / "config.toml"
    if backend == _OPENCODE:
        return Path.home() / ".config" / "opencode" / "opencode.json"
    return None


def _parse_toml(text: str) -> object:
    return tomllib.loads(text)


def _parse_json(text: str) -> object:
    parsed: object = json.loads(text)
    return parsed


def _load(path: Path, parse: Callable[[str], object]) -> dict[str, object] | str:
    """The parsed top-level object at *path*, or a fail-open reason string."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"{path} does not exist"
    except (OSError, UnicodeDecodeError) as exc:
        return f"{path} is unreadable ({exc})"
    try:
        parsed = parse(text)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        return f"{path} does not parse ({exc})"
    if not isinstance(parsed, dict):
        return f"{path} top level is not an object"
    return parsed


def _entry_state(entry: dict[object, object], label: str) -> tuple[McpState, str]:
    """Classify one declared server entry by its ``enabled`` flag."""
    enabled = entry.get(_ENABLED_KEY, True)
    if not isinstance(enabled, bool):
        return McpState.UNKNOWN, f"{label}.enabled is not a bool"
    if not enabled:
        return McpState.DISABLED, f"{label}.enabled is false"
    return McpState.ENABLED, f"{label} is declared"


def _server_state(
    data: dict[str, object], servers_key: str, server: str
) -> tuple[McpState, str]:
    """Classify *server* under *servers_key* in one parsed config object."""
    if servers_key not in data:
        return McpState.ABSENT, f"no {servers_key} section"
    container = data[servers_key]
    if not isinstance(container, dict):
        reason = (
            f"{servers_key} is null"
            if container is None
            else f"{servers_key} is not a table/object"
        )
        return McpState.UNKNOWN, reason
    label = f"{servers_key}.{server}"
    if server not in container:
        return McpState.ABSENT, f"{label} is not declared"
    entry = container[server]
    if not isinstance(entry, dict):
        reason = (
            f"{label} is null" if entry is None else f"{label} is not a table/object"
        )
        return McpState.UNKNOWN, reason
    return _entry_state(entry, label)


def _project_dirs(root: Path) -> list[Path]:
    """*root* and its ancestors up to the first one holding ``.git``.

    With no ``.git`` ancestor, the walk stops at ``Path.home()`` when that is
    an ancestor, else it runs to the filesystem root.
    """
    chain = [root, *root.parents]
    for index, directory in enumerate(chain):
        if (directory / _GIT_MARKER).exists():
            return chain[: index + 1]
    home = Path.home()
    if home in chain:
        return chain[: chain.index(home) + 1]
    return chain


def _other_layers(backend: str, root: Path, user_path: Path) -> str | None:
    """Name the first other config layer that exists, or ``None``.

    Stat only: no candidate is ever opened.
    """
    if backend == _OPENCODE:
        for name in _OPENCODE_ENV_VARS:
            if name in os.environ:
                return f"env {name} is set; not parsed"
    candidates: list[Path] = []
    if backend == _OPENCODE:
        candidates.append(user_path.with_suffix(".jsonc"))
    names = _PROJECT_LAYER_NAMES.get(backend, ())
    candidates.extend(d / name for d in _project_dirs(root) for name in names)
    candidates.extend(_SYSTEM_LAYERS.get(backend, ()))
    for candidate in candidates:
        if candidate != user_path and candidate.exists():
            return f"project/system layer present at {candidate}; not parsed"
    return None


def _layer_server_names(path: Path) -> tuple[str, ...] | None:
    """The ``mcp_servers`` names one codex config file declares, or ``None``.

    ``None`` for a missing or unreadable file, a parse error or a non-table
    section: the answer for that layer is unknown.
    """
    loaded = _load(path, _parse_toml)
    if isinstance(loaded, str):
        return None
    container = loaded.get(_CODEX_SERVERS_KEY, {})
    if not isinstance(container, dict):
        return None
    return tuple(str(name) for name in container)


def declared_server_names(backend: str) -> tuple[str, ...] | None:
    """The MCP server names *backend*'s user and system layers declare (#3057, #3368).

    Codex only: the user layer then the system layer (``/etc/codex/config.toml``),
    user names first, de-duplicated, in declaration order. A system-layer server
    is enabled for every codex turn unless disabled, so a caller that disables
    every name it gets must see those too. A project layer is not read: the
    tracker-IO turn's cwd is a cw-owned scratch directory, so none reaches it.

    ``()`` when a layer parses and has no ``mcp_servers`` section. ``None`` when
    no layer's answer is known (another backend, or every layer missing,
    unreadable, unparseable or holding a non-table section); callers disable
    nothing on ``None``.
    """
    if backend != _CODEX:
        return None
    user_path = user_layer_path(backend)
    paths = (*((user_path,) if user_path else ()), *_SYSTEM_LAYERS.get(backend, ()))
    answers = [names for names in map(_layer_server_names, paths) if names is not None]
    if not answers:
        return None
    return tuple(dict.fromkeys(name for names in answers for name in names))


def read_backend_mcp(backend: str, server: str, root: Path) -> McpConfigReading:
    """Read whether *backend*'s config declares MCP *server*.

    *root* is the client root the probe walks for project layers
    (``workspace_path`` on the dispatch tick, ``repo_path or workspace_path``
    in doctor).
    """
    path = user_layer_path(backend)
    if path is None:
        return McpConfigReading(
            McpState.UNKNOWN, Path(), f"no MCP config reader for backend {backend}"
        )
    is_codex = backend == _CODEX
    loaded = _load(path, _parse_toml if is_codex else _parse_json)
    if isinstance(loaded, str):
        return McpConfigReading(McpState.UNKNOWN, path, loaded)
    servers_key = _CODEX_SERVERS_KEY if is_codex else _OPENCODE_SERVERS_KEY
    state, reason = _server_state(loaded, servers_key, server)
    if state in (McpState.ABSENT, McpState.DISABLED):
        layer = _other_layers(backend, root, path)
        if layer is not None:
            return McpConfigReading(McpState.UNKNOWN, path, f"{reason}, but {layer}")
    return McpConfigReading(state, path, reason)
