"""Wire-format conformance tests for ``RealCmuxAdapter``.

A threaded Unix-socket server speaks the same JSON-RPC envelope as cmux
and lets us exercise the adapter's real I/O path — serialization, framing,
response parsing, and error propagation — without a live cmux daemon.

Each test is tagged ``@pytest.mark.cmux`` so it can be selected with
``pytest -m cmux`` for focused work; the marker does not exclude the
tests from default runs, so they participate in the regular CI matrix.

Replaces the parked nightly real-cmux job (see
``.github/workflows/nightly.yml``) for adapter-contract coverage. Whoever
stands up a self-hosted macOS runner can flip that workflow back on; in
the meantime this module is the only thing pinning the wire format.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from cw.cmux import RealCmuxAdapter
from cw.exceptions import CwError

HandlerResult = dict[str, Any]
Handler = Callable[[dict[str, Any]], HandlerResult]

_ACCEPT_TIMEOUT_S = 0.5
_THREAD_JOIN_TIMEOUT_S = 2.0
_RECV_CHUNK = 4096


class CmuxRPCError(Exception):
    """Raised inside a handler so the mock returns a daemon-reported error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class MockCmuxDaemon:
    """Threaded ``AF_UNIX`` server that emulates cmux's JSON-RPC dispatch."""

    def __init__(self, socket_path: Path, handlers: dict[str, Handler]) -> None:
        self.socket_path = socket_path
        self._handlers = handlers
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def start(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Periodic timeout so the accept loop can notice _stop without
        # needing a sentinel connection.
        self._server.settimeout(_ACCEPT_TIMEOUT_S)
        self._server.bind(str(self.socket_path))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._handle_connection(conn)
            finally:
                conn.close()

    def _handle_connection(self, conn: socket.socket) -> None:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(_RECV_CHUNK)
            if not chunk:
                return
            buf += chunk
        req = json.loads(buf.decode().strip())
        method = req["method"]
        params = req.get("params", {})
        self.requests.append((method, params))
        resp = self._dispatch(req.get("id"), method, params)
        conn.sendall((json.dumps(resp) + "\n").encode())

    def _dispatch(
        self, req_id: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        handler = self._handlers.get(method)
        if handler is None:
            return {
                "id": req_id,
                "ok": False,
                "error": {
                    "code": "method-unknown",
                    "message": f"no handler for {method}",
                },
            }
        try:
            result = handler(params)
        except CmuxRPCError as exc:
            return {
                "id": req_id,
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        return {"id": req_id, "ok": True, "result": result}


DaemonFactory = Callable[[dict[str, Handler]], MockCmuxDaemon]


@pytest.fixture
def mock_cmux_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[DaemonFactory]:
    """Spawn a fresh mock daemon per call; tear all of them down at exit."""
    daemons: list[MockCmuxDaemon] = []

    def factory(handlers: dict[str, Handler]) -> MockCmuxDaemon:
        # The wire path is OS-agnostic, so bypass the macOS guard once.
        monkeypatch.setattr("cw.cmux.sys.platform", "darwin")
        sock_path = tmp_path / f"cmux-{len(daemons)}.sock"
        daemon = MockCmuxDaemon(sock_path, handlers)
        daemon.start()
        daemons.append(daemon)
        return daemon

    yield factory

    for daemon in daemons:
        daemon.stop()


@pytest.mark.cmux
class TestRealCmuxAdapterWireFormat:
    """End-to-end: real Unix socket, real JSON serialization, mock daemon."""

    def test_spawn_round_trip(self, mock_cmux_daemon: DaemonFactory) -> None:
        """spawn() emits the documented RPC sequence and returns the new id."""
        daemon = mock_cmux_daemon(
            {
                "workspace.list": lambda _: {
                    "workspaces": [{"id": "ws-1", "title": "my-ws"}]
                },
                "surface.list": lambda _: {"surfaces": [{"id": "surf-1"}]},
                "surface.split": lambda _: {"surface_id": "surf-2"},
                "surface.send_text": lambda _: {},
            }
        )
        adapter = RealCmuxAdapter(socket_path=daemon.socket_path)

        result = adapter.spawn("my-ws", "claude", "right")

        assert result == "surf-2"
        assert [m for m, _ in daemon.requests] == [
            "workspace.list",
            "surface.list",
            "surface.split",
            "surface.send_text",
        ]
        split_params = next(p for m, p in daemon.requests if m == "surface.split")
        assert split_params == {
            "workspace_id": "ws-1",
            "direction": "right",
            "surface_id": "surf-1",
        }
        send_params = next(p for m, p in daemon.requests if m == "surface.send_text")
        assert send_params == {"surface_id": "surf-2", "text": "claude\n"}

    def test_list_surfaces_aggregates_across_workspaces(
        self, mock_cmux_daemon: DaemonFactory
    ) -> None:
        """list_surfaces unions surface ids across every workspace."""
        per_workspace = {
            "ws-a": {"surfaces": [{"id": "s1"}, {"id": "s2"}]},
            "ws-b": {"surfaces": [{"id": "s3"}]},
        }
        daemon = mock_cmux_daemon(
            {
                "workspace.list": lambda _: {
                    "workspaces": [{"id": "ws-a"}, {"id": "ws-b"}]
                },
                "surface.list": lambda params: per_workspace[params["workspace_id"]],
            }
        )
        adapter = RealCmuxAdapter(socket_path=daemon.socket_path)

        assert adapter.list_surfaces() == {"s1", "s2", "s3"}

    def test_daemon_error_propagates_as_cw_error(
        self, mock_cmux_daemon: DaemonFactory
    ) -> None:
        """A daemon-reported ``ok: false`` response raises ``CwError``."""

        def fail(_: dict[str, Any]) -> HandlerResult:
            raise CmuxRPCError("bad-request", "missing required param")

        daemon = mock_cmux_daemon({"system.identify": fail})
        adapter = RealCmuxAdapter(socket_path=daemon.socket_path)

        with pytest.raises(CwError, match=r"bad-request.*missing required param"):
            adapter.identify()
