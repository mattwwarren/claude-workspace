"""Terminal multiplexer adapters and factory.

Defines the :class:`MultiplexerAdapter` protocol that every backend
implements, plus the macOS-native :class:`RealCmuxAdapter` and the
test-only :class:`FakeCmuxAdapter`. :func:`get_backend_adapter` is the
one factory callers should use; legacy names (``CmuxAdapter``,
``get_cmux_adapter``) are kept as aliases for one release.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from cw.config import load_orchestrator_config
from cw.exceptions import CwError
from cw.models import BackendName
from cw.tmux import TmuxAdapter

_LEGACY_HINT_PATH: Path = Path("/tmp/cmux-last-socket-path")


def _find_socket() -> Path:
    """Discover cmux socket path in priority order."""
    if path := os.environ.get("CMUX_SOCKET_PATH"):
        return Path(path)
    if path := os.environ.get("CMUX_SOCKET"):
        return Path(path)
    if tag := os.environ.get("CMUX_TAG"):
        return Path(f"/tmp/cmux-debug-{tag}.sock")
    stable = Path.home() / "Library" / "Application Support" / "cmux" / "cmux.sock"
    if stable.exists():
        return stable
    hint_files = [
        Path.home() / "Library" / "Application Support" / "cmux" / "last-socket-path",
        _LEGACY_HINT_PATH,
    ]
    for hint_file in hint_files:
        if hint_file.exists():
            candidate = Path(hint_file.read_text().strip())
            if candidate.exists():
                return candidate
    return Path("/tmp/cmux.sock")


@runtime_checkable
class MultiplexerAdapter(Protocol):
    """Protocol for terminal-multiplexer backends (cmux, tmux, fakes)."""

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

    def list_surfaces(self) -> set[str]:
        """Return the set of surface refs currently known to the backend.

        Used by reconciliation to detect phantom sessions: any surface_ref
        in cw state that is *not* in this set is assumed dead. The contract
        is all-or-nothing — return a complete live set, or an empty set
        if the backend is unreachable or only partially enumerable.
        Partial results would cause selective false-positive reaping, so
        implementers must not return a subset. ``cw.reconcile`` treats an
        empty result as "possibly unreachable" and refuses to touch state
        when any known session still has a surface_ref.
        """
        ...


# Legacy alias. Keep for one release so downstream type hints that read
# `from cw.cmux import CmuxAdapter` don't break mid-upgrade.
CmuxAdapter = MultiplexerAdapter


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
        """Send a JSON-RPC request and return the result dict.

        All failure modes — socket unreachable, short read, malformed JSON,
        daemon-reported error — are normalised to ``CwError`` so callers can
        rely on a single exception type.
        """
        self._counter += 1
        req = json.dumps({"id": self._counter, "method": method, "params": params})
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                sock.connect(str(self._socket_path))
                sock.sendall((req + "\n").encode())
                response = b""
                while not response.endswith(b"\n"):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            except OSError as exc:
                msg = f"cmux socket error at {self._socket_path}: {exc}"
                raise CwError(msg) from exc
        finally:
            sock.close()
        try:
            raw: dict[str, Any] = json.loads(response.decode().strip())
        except json.JSONDecodeError as exc:
            msg = f"cmux returned malformed JSON: {exc}"
            raise CwError(msg) from exc
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
        surfaces = self._call("surface.list", {"workspace_id": ws_id}).get(
            "surfaces", []
        )
        split_params: dict[str, Any] = {
            "workspace_id": ws_id,
            "direction": surface,
        }
        if surfaces:
            split_params["surface_id"] = surfaces[0]["id"]
        split = self._call("surface.split", split_params)
        surf_id: str = split["surface_id"]
        self._call("surface.send_text", {"surface_id": surf_id, "text": f"{command}\n"})
        return surf_id

    def close(self, surface_ref: str) -> None:
        """Close the surface identified by surface_ref."""
        self._call("surface.close", {"surface_id": surface_ref})

    def identify(self) -> dict[str, Any]:
        """Return current focus context from system.identify."""
        return self._call("system.identify", {})

    def list_surfaces(self) -> set[str]:
        """Return the set of live cmux surface IDs across all workspaces.

        Returns an empty set on any enumeration failure (socket down,
        ``workspace.list`` errors, or any ``surface.list`` call fails).
        Partial results would let the reconciler falsely flag surfaces
        from the failing workspace as phantom while preserving the rest,
        so the adapter gives an all-or-nothing answer.
        """
        try:
            workspaces_raw = self._call("workspace.list", {})
            workspaces: list[dict[str, Any]] = workspaces_raw.get("workspaces", [])
            live: set[str] = set()
            for ws in workspaces:
                ws_id = ws.get("id")
                if not ws_id:
                    continue
                resp = self._call("surface.list", {"workspace_id": ws_id})
                for surf in resp.get("surfaces", []):
                    surf_id = surf.get("id")
                    if surf_id:
                        live.add(surf_id)
        except CwError:
            return set()
        return live


class FakeCmuxAdapter:
    """In-memory adapter for testing. Records all calls; no real I/O."""

    def __init__(self) -> None:
        self._counter = 0
        self.calls: dict[str, list[tuple[object, ...]]] = {
            "spawn": [],
            "close": [],
            "identify": [],
            "list_surfaces": [],
        }
        self._live: set[str] = set()

    def spawn(self, workspace: str, command: str, surface: str = "right") -> str:
        """Record call and return a deterministic fake surface ref."""
        self._counter += 1
        ref = f"fake-pane-{self._counter}"
        self.calls["spawn"].append((workspace, command, surface))
        self._live.add(ref)
        return ref

    def close(self, surface_ref: str) -> None:
        """Record call and drop from live set (idempotent)."""
        self.calls["close"].append((surface_ref,))
        self._live.discard(surface_ref)

    def identify(self) -> dict[str, Any]:
        """Record call and return stub focus context."""
        self.calls["identify"].append(())
        return {
            "focused": {
                "workspace_id": "fake-ws-1",
                "surface_id": "fake-pane-1",
            }
        }

    def list_surfaces(self) -> set[str]:
        """Return the current in-memory live-surface set (copy)."""
        self.calls["list_surfaces"].append(())
        return set(self._live)


def _resolve_backend_name() -> BackendName:
    """Walk the three-tier backend selector and return the chosen name.

    Priority:
    1. ``CW_BACKEND`` env var — testing / CI override, highest priority.
    2. ``orchestrator.yaml`` ``backend:`` field — persistent user choice.
    3. Platform default — ``darwin`` picks cmux, everything else tmux.
    """
    env_choice = os.environ.get("CW_BACKEND", "").strip().lower()
    if env_choice:
        try:
            return BackendName(env_choice)
        except ValueError as exc:
            valid = ", ".join(b.value for b in BackendName)
            msg = f"Invalid CW_BACKEND={env_choice!r}; valid values: {valid}"
            raise CwError(msg) from exc

    try:
        config = load_orchestrator_config()
    except Exception:  # pragma: no cover - config load shouldn't hard-fail selector
        config = None
    if config is not None and config.backend is not None:
        return config.backend

    return BackendName.CMUX if sys.platform == "darwin" else BackendName.TMUX


def get_backend_adapter() -> MultiplexerAdapter:
    """Return the active multiplexer adapter.

    Walks the three-tier selector (env → config → platform default).
    On ``CW_BACKEND=fake`` returns a :class:`FakeCmuxAdapter` so CI and
    local smoke tests can skip the real multiplexer entirely.
    """
    name = _resolve_backend_name()
    if name is BackendName.CMUX:
        return RealCmuxAdapter()
    if name is BackendName.TMUX:
        return TmuxAdapter()
    if name is BackendName.FAKE:
        return FakeCmuxAdapter()
    msg = f"Unhandled backend selector: {name!r}"  # pragma: no cover - enum exhausted
    raise CwError(msg)


# Legacy alias retained for one release alongside ``CmuxAdapter``.
get_cmux_adapter = get_backend_adapter
