"""Stage codex / opencode MCP config files for tests (#3055).

One home for writing a backend's user-layer config in a known state, shared by
the reader, gate and doctor tests. ``enabled``, ``absent`` and ``disabled``
derive from the committed redacted captures in ``tests/fixtures/backend_mcp/``
by text edits only (``absent`` drops the server entry, ``disabled`` adds
``enabled = false``); the capture itself is never tidied. The other states are
negative cases about our own parser, so they are small inline literals.

Paths: codex writes under a ``CODEX_HOME`` set to a ``tmp_path`` directory;
opencode writes under the per-test ``Path.home()`` (HOME is isolated by the
autouse ``_isolate_home`` fixture).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import cw.backend_mcp_config
from cw.models.orchestrator_config import CODEX_BACKEND, OPENCODE_BACKEND

if TYPE_CHECKING:
    import pytest

McpStage = Literal[
    "enabled",
    "absent",
    "disabled",
    "missing",
    "malformed",
    "non_dict",
    "non_bool_enabled",
    "jsonc",
]

_FIXTURES = Path(__file__).parent / "fixtures" / "backend_mcp"
_CODEX_CAPTURE = _FIXTURES / "codex_config.toml"
_OPENCODE_CAPTURE = _FIXTURES / "opencode.json"
# The server name the captures carry.
_CAPTURED_SERVER = "linear"


def _codex_text(state: McpStage, server: str) -> str:
    capture = _CODEX_CAPTURE.read_text(encoding="utf-8").replace(
        f"[mcp_servers.{_CAPTURED_SERVER}]", f"[mcp_servers.{server}]"
    )
    header = f"[mcp_servers.{server}]\n"
    texts = {
        "enabled": capture,
        "absent": re.sub(
            rf"\[mcp_servers\.{re.escape(server)}\]\n(?:[^\[\n].*\n)*\n?",
            "",
            capture,
        ),
        "disabled": capture.replace(header, f"{header}enabled = false\n"),
        "malformed": "this is not [valid toml\n",
        "non_dict": 'mcp_servers = "not-a-table"\n',
        "non_bool_enabled": f'{header}command = "x"\nenabled = "no"\n',
    }
    return texts[state]


def _opencode_text(state: McpStage, server: str) -> str:
    capture = _OPENCODE_CAPTURE.read_text(encoding="utf-8").replace(
        f'"{_CAPTURED_SERVER}": {{', f'"{server}": {{'
    )
    entry_open = f'"{server}": {{\n'
    texts = {
        "enabled": capture,
        "absent": re.sub(
            rf'\n    "{re.escape(server)}": \{{\n(?:      .*\n)*    \}}\n',
            "\n",
            capture,
        ),
        "disabled": capture.replace(
            entry_open, f'{entry_open}      "enabled": false,\n'
        ),
        "malformed": '{"mcp": {',
        "non_dict": '{"mcp": []}\n',
        "non_bool_enabled": f'{{"mcp": {{"{server}": {{"enabled": "no"}}}}}}\n',
        "jsonc": f'{{\n  // comment\n  "mcp": {{"{server}": {{"type": "x"}}}}\n}}\n',
    }
    return texts[state]


def opencode_user_path() -> Path:
    """The opencode user-layer path under the per-test HOME."""
    return Path.home() / ".config" / "opencode" / "opencode.json"


def stage_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: str,
    state: McpStage,
    server: str = _CAPTURED_SERVER,
) -> Path:
    """Write *backend*'s user-layer config in *state*; return the path read.

    ``missing`` writes nothing (the path is still returned). ``jsonc`` is
    opencode only.
    """
    if backend == CODEX_BACKEND:
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        path = codex_home / "config.toml"
        text = None if state == "missing" else _codex_text(state, server)
    elif backend == OPENCODE_BACKEND:
        path = opencode_user_path()
        text = None if state == "missing" else _opencode_text(state, server)
    else:
        msg = f"no MCP config staging for backend {backend!r}"
        raise ValueError(msg)
    if text is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path


def write_project_layer(root: Path, backend: str) -> Path:
    """Create a project-layer config file for *backend* under *root*."""
    if backend == CODEX_BACKEND:
        path = root / ".codex" / "config.toml"
    else:
        path = root / "opencode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def write_system_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> Path:
    """Create a fake system-layer file and point ``_SYSTEM_LAYERS`` at it."""
    path = tmp_path / "etc" / backend / "config"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cw.backend_mcp_config, "_SYSTEM_LAYERS", {backend: (path,)})
    return path
