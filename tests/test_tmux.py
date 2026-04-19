"""Tests for cw.tmux - tmux multiplexer adapter."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.tmux import TmuxAdapter

if TYPE_CHECKING:
    from collections.abc import Callable


class TestInstantiationGuard:
    def test_missing_tmux_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.tmux.shutil.which", lambda _name: None)
        with pytest.raises(CwError, match="tmux not found"):
            TmuxAdapter()

    def test_tmux_on_path_instantiates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.tmux.shutil.which", lambda _name: "/usr/bin/tmux")
        adapter = TmuxAdapter()
        assert isinstance(adapter, TmuxAdapter)


class TestSubprocessWiring:
    """Verify the adapter calls tmux with the expected arguments.

    These tests stub out ``subprocess.run`` so they work on any platform
    and don't require a running tmux server.
    """

    def _make_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[
        TmuxAdapter,
        list[list[str]],
        Callable[..., subprocess.CompletedProcess[str]],
    ]:
        monkeypatch.setattr("cw.tmux.shutil.which", lambda _name: "/usr/bin/tmux")
        adapter = TmuxAdapter()
        calls: list[list[str]] = []

        def fake_run(
            args: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(list(args))
            # First call is has-session (may fail); subsequent calls succeed.
            # Simulate the pane ref coming back from split-window -P.
            if "split-window" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="my-ws:0.1\n", stderr=""
                )
            if "has-session" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr=""
                )
            if "display-message" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout='{"focused":{"workspace_id":"my-ws","surface_id":"my-ws:0.1"}}\n',
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr("cw.tmux.subprocess.run", fake_run)
        return adapter, calls, fake_run

    def test_spawn_creates_session_when_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, calls, _ = self._make_adapter(monkeypatch)
        adapter.spawn("my-ws", "claude", "right")

        has = next(c for c in calls if "has-session" in c)
        assert "my-ws" in has
        new = next(c for c in calls if "new-session" in c)
        assert "my-ws" in new

    def test_spawn_returns_pane_ref_and_sends_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, calls, _ = self._make_adapter(monkeypatch)
        ref = adapter.spawn("my-ws", "claude", "right")
        assert ref == "my-ws:0.1"

        send = [c for c in calls if "send-keys" in c]
        # One call to send the literal command, one to press Enter.
        assert len(send) == 2
        literal = send[0]
        assert "-l" in literal
        assert "claude" in literal
        enter = send[1]
        assert "Enter" in enter

    def test_spawn_maps_surface_to_split_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, calls, _ = self._make_adapter(monkeypatch)
        adapter.spawn("my-ws", "claude", "bottom")
        split = next(c for c in calls if "split-window" in c)
        assert "-v" in split

    def test_close_kills_pane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter, calls, _ = self._make_adapter(monkeypatch)
        adapter.close("my-ws:0.1")
        kill = next(c for c in calls if "kill-pane" in c)
        assert "my-ws:0.1" in kill

    def test_identify_parses_display_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, _calls, _ = self._make_adapter(monkeypatch)
        result = adapter.identify()
        assert result["focused"]["workspace_id"] == "my-ws"
        assert result["focused"]["surface_id"] == "my-ws:0.1"

    def test_identify_survives_detached_tmux(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.tmux.shutil.which", lambda _name: "/usr/bin/tmux")
        adapter = TmuxAdapter()

        def fake_run(
            args: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="not attached"
            )

        monkeypatch.setattr("cw.tmux.subprocess.run", fake_run)
        assert adapter.identify() == {"focused": {}}


def test_tmux_list_surfaces_parses_pane_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_surfaces parses `tmux list-panes -a -F` output into a set."""
    monkeypatch.setattr("cw.tmux.shutil.which", lambda _: "/usr/bin/tmux")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:4] == ["tmux", "list-panes", "-a", "-F"]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="cw-client-a:0.0\ncw-client-a:0.1\ncw-client-b:1.0\n",
            stderr="",
        )

    monkeypatch.setattr("cw.tmux.subprocess.run", fake_run)
    adapter = TmuxAdapter()

    assert adapter.list_surfaces() == {
        "cw-client-a:0.0",
        "cw-client-a:0.1",
        "cw-client-b:1.0",
    }


def test_tmux_list_surfaces_returns_empty_on_server_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If tmux server is not running, list-panes returns non-zero; we return empty."""
    monkeypatch.setattr("cw.tmux.shutil.which", lambda _: "/usr/bin/tmux")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="no server running on /tmp/tmux-1000/default\n",
        )

    monkeypatch.setattr("cw.tmux.subprocess.run", fake_run)
    adapter = TmuxAdapter()
    assert adapter.list_surfaces() == set()


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestTmuxIntegration:
    """End-to-end against a real tmux server on an isolated socket.

    Runs in its own ``-L`` socket so it never collides with a tmux
    session the developer has attached interactively.
    """

    def test_spawn_close_cycle(self) -> None:
        socket_name = f"cw-test-{uuid.uuid4().hex[:8]}"
        ws = f"cw-test-{uuid.uuid4().hex[:8]}"
        try:
            # Pre-flight: tmux binary is responsive.
            subprocess.run(
                ["tmux", "-L", socket_name, "kill-server"],
                check=False,
                capture_output=True,
            )
            adapter = TmuxAdapter()
            # Shim _run to target the isolated socket for every call.
            original = adapter._run

            def socketed_run(
                args: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                return original(["-L", socket_name, *args], **kwargs)  # type: ignore[arg-type]

            adapter._run = socketed_run  # type: ignore[method-assign]

            ref = adapter.spawn(ws, "true", "right")
            assert ref.startswith(f"{ws}:")
            adapter.close(ref)
        finally:
            subprocess.run(
                ["tmux", "-L", socket_name, "kill-server"],
                check=False,
                capture_output=True,
            )
