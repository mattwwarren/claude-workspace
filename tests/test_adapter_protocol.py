"""Protocol conformance tests parametrized over every known adapter.

Each backend must satisfy the :class:`MultiplexerAdapter` protocol —
same method names, same arities, same basic return types. Fast to run
(no real multiplexer required); catches drift when a backend adds an
argument the protocol hasn't grown yet.
"""

from __future__ import annotations

import inspect
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.cmux import FakeCmuxAdapter, MultiplexerAdapter, RealCmuxAdapter
from cw.tmux import TmuxAdapter

if TYPE_CHECKING:
    pass


def _instantiate[A: MultiplexerAdapter](
    cls: type[A], monkeypatch: pytest.MonkeyPatch
) -> A:
    """Return a usable instance without requiring a live backend."""
    if cls is FakeCmuxAdapter:
        return cls()
    if cls is TmuxAdapter:
        monkeypatch.setattr("cw.tmux.shutil.which", lambda _name: "/usr/bin/tmux")
        return cls()
    if cls is RealCmuxAdapter:
        # Skip the macOS guard by stubbing sys.platform before __init__
        # reads it; we're only inspecting signatures, not running spawn.
        monkeypatch.setattr("cw.cmux.sys.platform", "darwin")
        return cls()
    msg = f"unhandled adapter class: {cls.__name__}"
    raise AssertionError(msg)


ADAPTER_CLASSES = [FakeCmuxAdapter, TmuxAdapter, RealCmuxAdapter]


@pytest.mark.parametrize("adapter_cls", ADAPTER_CLASSES)
class TestProtocolConformance:
    def test_satisfies_runtime_protocol(
        self, adapter_cls: type[MultiplexerAdapter], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = _instantiate(adapter_cls, monkeypatch)
        assert isinstance(adapter, MultiplexerAdapter)

    def test_spawn_signature(self, adapter_cls: type[MultiplexerAdapter]) -> None:
        sig = inspect.signature(adapter_cls.spawn)
        params = list(sig.parameters.keys())
        # self, workspace, command, surface
        assert params[1:] == ["workspace", "command", "surface"]
        assert sig.parameters["surface"].default == "right"

    def test_close_signature(self, adapter_cls: type[MultiplexerAdapter]) -> None:
        sig = inspect.signature(adapter_cls.close)
        params = list(sig.parameters.keys())
        assert params[1:] == ["surface_ref"]

    def test_identify_signature(self, adapter_cls: type[MultiplexerAdapter]) -> None:
        sig = inspect.signature(adapter_cls.identify)
        params = list(sig.parameters.keys())
        # self only
        assert params == ["self"]

    def test_capture_surface_signature(
        self, adapter_cls: type[MultiplexerAdapter]
    ) -> None:
        sig = inspect.signature(adapter_cls.capture_surface)
        # Strip leading underscore to allow raise-only stubs to use _param naming
        params = [p.lstrip("_") for p in sig.parameters]
        # self, surface_ref, lines, scrollback
        assert params[1:] == ["surface_ref", "lines", "scrollback"]


class TestSpawnReturnTypesMatch:
    """The callable adapters (Fake, Tmux) must both return a string ref.

    RealCmuxAdapter.spawn needs a live socket, so it's excluded — its
    signature is covered in the parametrized class above.
    """

    def test_fake_spawn_returns_string(self) -> None:
        ref = FakeCmuxAdapter().spawn("ws", "claude")
        assert isinstance(ref, str)
        assert ref

    def test_tmux_spawn_returns_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.tmux.shutil.which", lambda _name: "/usr/bin/tmux")

        def fake_run(
            args: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if "split-window" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="ws:0.1\n", stderr=""
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr("cw.tmux.subprocess.run", fake_run)
        adapter = TmuxAdapter()
        ref = adapter.spawn("ws", "claude")
        assert isinstance(ref, str)
        assert ref == "ws:0.1"
