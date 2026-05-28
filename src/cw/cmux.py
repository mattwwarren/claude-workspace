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

import yaml
from pydantic import ValidationError

from cw._util import _tail_lines
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

    def list_live_surface_commands(self) -> dict[str, str]:
        """Return a mapping of surface_ref to foreground command name.

        Used as a second-pass filter in reconciliation to detect zombie
        panes whose surface still exists but whose claude process has
        exited (the pane is back at the shell prompt). Returns the same
        keys as :meth:`list_surfaces`.

        Same all-or-nothing contract as :meth:`list_surfaces`: return an
        empty dict if the backend cannot enumerate commands reliably. An
        empty return is treated by reconcile as "command info
        unavailable; skip command filter" — fail-open, no false-positive
        reaping.

        For backends where command introspection is unsupported (cmux),
        return every live surface mapped to a non-shell sentinel so the
        zombie filter is a transparent no-op for those surfaces.
        """
        ...

    def inspect_pane(self, surface_ref: str) -> dict[str, Any]:
        """Return pane info best-effort; {} if unavailable."""
        ...

    def capture_surface(self, surface_ref: str, lines: int, scrollback: int) -> str:
        """Return last *lines* lines of worker output for *surface_ref*.

        Looks back at most *scrollback* lines. Raises CwError when unavailable.
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

    def list_live_surface_commands(self) -> dict[str, str]:
        """Return a no-op command map for cmux surfaces.

        The cmux ``surface.list`` JSON-RPC response does not expose a
        current-command or process-name field — only ``id``, ``ref``,
        ``index``, ``type``, ``focused``, ``title`` (see
        ``docs/cmux-protocol.md``). Until cmux exposes process info, every
        live cmux surface is reported as running a non-shell sentinel so
        the zombie-pane filter is transparent for the cmux backend. See
        GitHub issue #144.
        """
        return dict.fromkeys(self.list_surfaces(), "cmux-surface")

    def inspect_pane(self, _surface_ref: str) -> dict[str, Any]:
        """Return empty dict — cmux does not expose pane activity info."""
        return {}

    def capture_surface(self, _surface_ref: str, _lines: int, _scrollback: int) -> str:
        """Raise CwError — cmux does not support output capture.

        Switch to the tmux backend (``CW_BACKEND=tmux``) or use
        ``cw post-mortem`` once that command is available.
        """
        msg = (
            "capture_surface is not supported by the cmux backend;"
            " switch to tmux (CW_BACKEND=tmux) or use cw post-mortem."
        )
        raise CwError(msg)


class FakeCmuxAdapter:
    """In-memory adapter for testing. Records all calls; no real I/O."""

    def __init__(self) -> None:
        self._counter = 0
        self.calls: dict[str, list[tuple[object, ...]]] = {
            "spawn": [],
            "close": [],
            "identify": [],
            "list_surfaces": [],
            "list_live_surface_commands": [],
            "inspect_pane": [],
        }
        self.capture_calls: list[dict[str, object]] = []
        self._live: set[str] = set()
        self._live_commands: dict[str, str] = {}
        self._commands_fail: bool = False
        self._surface_content: dict[str, str] = {}
        self._pane_info: dict[str, dict[str, Any]] = {}

    def spawn(self, workspace: str, command: str, surface: str = "right") -> str:
        """Record call and return a deterministic fake surface ref."""
        self._counter += 1
        ref = f"fake-pane-{self._counter}"
        self.calls["spawn"].append((workspace, command, surface))
        self._live.add(ref)
        self._live_commands[ref] = "claude"
        return ref

    def close(self, surface_ref: str) -> None:
        """Record call and drop from live set (idempotent)."""
        self.calls["close"].append((surface_ref,))
        self._live.discard(surface_ref)
        self._live_commands.pop(surface_ref, None)

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

    def list_live_surface_commands(self) -> dict[str, str]:
        """Return a copy of the current foreground-command map.

        If ``_commands_fail`` is True, returns an empty dict to simulate a
        backend enumeration failure (exercises the fail-open path in
        :func:`cw.reconcile.compute_drift`).
        """
        self.calls["list_live_surface_commands"].append(())
        if self._commands_fail:
            return {}
        return dict(self._live_commands)

    def set_pane_command(self, surface_ref: str, command: str) -> None:
        """Override the foreground command for a surface (test helper).

        No assertion that the ref is in the live set — keeps test helpers
        flexible for exercising edge-case scenarios.
        """
        self._live_commands[surface_ref] = command

    def capture_surface(self, surface_ref: str, lines: int, scrollback: int) -> str:
        """Return last *lines* lines of stored content for *surface_ref*.

        Records the call in ``calls["capture_surface"]``. Raises CwError
        when the ref is not in the live set.
        """
        self.capture_calls.append(
            {"surface_ref": surface_ref, "lines": lines, "scrollback": scrollback}
        )
        if surface_ref not in self._live:
            msg = f"Surface '{surface_ref}' is not active."
            raise CwError(msg)
        content = self._surface_content.get(surface_ref, "")
        return _tail_lines(content, lines)

    def set_surface_content(self, surface_ref: str, content: str) -> None:
        """Set the stored output content for a surface (test helper)."""
        self._surface_content[surface_ref] = content

    def inspect_pane(self, surface_ref: str) -> dict[str, Any]:
        """Return stored pane info for *surface_ref*, or {} if unknown."""
        self.calls["inspect_pane"].append((surface_ref,))  # tuple, like all other calls
        return dict(self._pane_info.get(surface_ref, {}))

    def set_pane_info(self, surface_ref: str, data: dict[str, Any]) -> None:
        """Configure the value returned by inspect_pane (test helper)."""
        self._pane_info[surface_ref] = data


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
    except (
        OSError,
        yaml.YAMLError,
        ValidationError,
    ):  # pragma: no cover - config load shouldn't hard-fail selector
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
