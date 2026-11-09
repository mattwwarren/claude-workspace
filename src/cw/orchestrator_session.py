"""Per-session ``cw-operator`` wiring for ``cw orchestrator-start`` (#3170).

The orchestrator session subscribes to the ``cw-operator`` attention channel
through a per-session ``--mcp-config`` file instead of the repo's shared
``.mcp.json``, so no other session in the workspace loads the server. The file
lives at ``<state_dir>/orchestrator/<session_id>.mcp.json``, keyed on the cw
session id so ``cw spawn close`` can find and remove it.

The file has exactly the shape of ``config/cw-operator-events.mcp.json.example``.
``--strict-mcp-config`` is not passed, so the session keeps its normal servers.

A cheap TCP probe of the channel server's host and port lets the launcher warn
when the server is down; the session still starts and falls back to the
``cw orchestrate watch`` Monitor.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.parse
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.config import state_dir
from cw.cw_operator_events_channel import BASE_URL_ENV, resolve_base_url

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

ORCHESTRATOR_AGENT = "cw-orchestrator"
ORCHESTRATOR_MCP_SERVER = "cw-operator"
ORCHESTRATOR_CHANNEL = f"server:{ORCHESTRATOR_MCP_SERVER}"

_CONFIG_DIR_NAME = "orchestrator"
_CONFIG_SUFFIX = ".mcp.json"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_PROBE_TIMEOUT_S = 1.0


def orchestrator_mcp_config_path(session_id: str) -> Path:
    """Return the per-session MCP config path for cw session *session_id*.

    Resolved through :func:`cw.config.state_dir` at call time, never bound at
    import, so the test suite's state-dir redirection applies.
    """
    return state_dir() / _CONFIG_DIR_NAME / f"{session_id}{_CONFIG_SUFFIX}"


def build_orchestrator_mcp_config(client_name: str, base_url: str) -> dict[str, object]:
    """Return the MCP config declaring one ``cw-operator`` proxy scoped to a client."""
    return {
        "mcpServers": {
            ORCHESTRATOR_MCP_SERVER: {
                "command": "cw",
                "args": ["operator-channel", "proxy", "--client-id", client_name],
                "env": {BASE_URL_ENV: base_url},
            }
        }
    }


def write_orchestrator_mcp_config(session_id: str, client_name: str) -> Path:
    """Write the per-session MCP config for *session_id* and return its path.

    The base URL is :func:`resolve_base_url`'s, the same value the launcher
    probes, so the proxy connects where the probe looked.
    """
    path = orchestrator_mcp_config_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    config = build_orchestrator_mcp_config(client_name, resolve_base_url())
    atomic_write_text(path, json.dumps(config, indent=2) + "\n")
    return path


def remove_orchestrator_mcp_config(session_id: str) -> bool:
    """Remove *session_id*'s per-session MCP config; idempotent.

    Returns ``True`` when the file is gone (removed now or never there) and
    ``False`` when the unlink failed. A failure is logged and swallowed: the
    file is a small leftover, never a reason to fail a ``cw spawn close``.
    """
    path = orchestrator_mcp_config_path(session_id)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "could not remove orchestrator MCP config %s for session %s: %s",
            path,
            session_id,
            exc,
        )
        return False
    return True


def orchestrator_launch_args(config_path: Path) -> list[str]:
    """Return the ``claude --bg`` arguments that load the agent and the channel."""
    return [
        "--agent",
        ORCHESTRATOR_AGENT,
        "--mcp-config",
        str(config_path),
        "--dangerously-load-development-channels",
        ORCHESTRATOR_CHANNEL,
    ]


def _host_port(base_url: str) -> tuple[str, int]:
    """Return *base_url*'s host and port; raise ``ValueError`` when it has none."""
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname
    if not host:
        msg = f"no host in {base_url!r}"
        raise ValueError(msg)
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    if port is None:
        msg = f"no port in {base_url!r}"
        raise ValueError(msg)
    return host, port


def operator_channel_reachable(
    base_url: str, *, timeout: float = _PROBE_TIMEOUT_S
) -> bool:
    """Return whether a TCP connection to *base_url*'s host and port succeeds.

    A connect probe only: it proves something listens, not that it is the
    queue-events server. An unparseable URL counts as unreachable.
    """
    try:
        host, port = _host_port(base_url)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def unreachable_warning(base_url: str) -> str:
    """The stderr line printed when the channel server does not answer."""
    try:
        host, port = _host_port(base_url)
    except ValueError:
        serve = "cw queue-channel serve"
    else:
        serve = f"cw queue-channel serve --host {host} --port {port}"
    return (
        f"warning: cw-operator channel server not reachable at {base_url}; "
        f"start it with: {serve}. The session will still start and falls back "
        "to the cw orchestrate watch Monitor."
    )
