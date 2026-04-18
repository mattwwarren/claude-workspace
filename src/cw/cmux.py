"""cmux terminal multiplexer adapter.

Platform: macOS-only for RealCmuxAdapter. All logic is testable on Linux
via FakeCmuxAdapter injection.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from cw.exceptions import CwError


def _find_socket() -> Path:
    """Discover cmux socket path in priority order."""
    if path := os.environ.get("CMUX_SOCKET_PATH"):
        return Path(path)
    if tag := os.environ.get("CMUX_TAG"):
        return Path(f"/tmp/cmux-{tag}.sock")
    stable = Path.home() / "Library" / "Application Support" / "cmux" / "cmux.sock"
    if stable.exists():
        return stable
    return Path("/tmp/cmux.sock")


@runtime_checkable
class CmuxAdapter(Protocol):
    """Protocol for cmux terminal multiplexer adapters."""

    def spawn(self, workspace: str, command: str, surface: str = "right") -> str:
        """Spawn a new surface in the given workspace running command.

        Returns the surface_id as a string reference.
        """
        ...

    def close(self, surface_ref: str) -> None:
        """Close the surface identified by surface_ref."""
        ...

    def identify(self) -> dict[str, Any]:
        """Return current focus context."""
        ...


class RealCmuxAdapter:
    """Real cmux adapter using Unix socket JSON-RPC. macOS only.

    Raises CwError on non-macOS platforms at instantiation time.
    """

    def __init__(self, socket_path: Path | None = None) -> None:
        if sys.platform != "darwin":
            msg = "RealCmuxAdapter requires macOS"
            raise CwError(msg)
        self._socket_path: Path = socket_path or _find_socket()
        self._counter: int = 0

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return the result dict."""
        self._counter += 1
        req = json.dumps({"id": self._counter, "method": method, "params": params})
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self._socket_path))
            sock.sendall((req + "\n").encode())
            response = b""
            while not response.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            sock.close()
        raw: dict[str, Any] = json.loads(response.decode().strip())
        if not raw.get("ok", True):
            err: dict[str, Any] = raw.get("error", {})
            msg = f"cmux error {err.get('code', 'unknown')}: {err.get('message', '')}"
            raise CwError(msg)
        return cast("dict[str, Any]", raw.get("result", {}))

    def spawn(self, workspace: str, command: str, surface: str = "right") -> str:
        """Find workspace, create a split, send the command. Returns surface_id."""
        workspaces = self._call("workspace.list", {}).get("workspaces", [])
        ws_id = next(
            (
                ws["id"]
                for ws in workspaces
                if ws.get("title") == workspace or ws.get("id") == workspace
            ),
            None,
        )
        if ws_id is None:
            msg = f"cmux workspace not found: {workspace!r}"
            raise CwError(msg)
        split = self._call(
            "surface.split", {"workspace_id": ws_id, "direction": surface}
        )
        surf_id: str = split["surface_id"]
        self._call("surface.send_text", {"surface_id": surf_id, "text": f"{command}\n"})
        return surf_id

    def close(self, surface_ref: str) -> None:
        """Close the surface identified by surface_ref."""
        self._call("surface.close", {"surface_id": surface_ref})

    def identify(self) -> dict[str, Any]:
        """Return current focus context from system.identify."""
        return self._call("system.identify", {})


class FakeCmuxAdapter:
    """In-memory adapter for testing. Records all calls; no real I/O."""

    def __init__(self) -> None:
        self._counter = 0
        self.calls: dict[str, list[tuple[object, ...]]] = {
            "spawn": [],
            "close": [],
            "identify": [],
        }

    def spawn(self, workspace: str, command: str, surface: str = "right") -> str:
        """Record call and return a deterministic fake surface ref."""
        self._counter += 1
        ref = f"fake-pane-{self._counter}"
        self.calls["spawn"].append((workspace, command, surface))
        return ref

    def close(self, surface_ref: str) -> None:
        """Record call."""
        self.calls["close"].append((surface_ref,))

    def identify(self) -> dict[str, Any]:
        """Record call and return stub focus context."""
        self.calls["identify"].append(())
        return {
            "focused": {
                "workspace_id": "fake-ws-1",
                "surface_id": "fake-pane-1",
            }
        }


def get_cmux_adapter() -> CmuxAdapter:
    """Return RealCmuxAdapter on macOS. Raises CwError on other platforms."""
    return RealCmuxAdapter()
