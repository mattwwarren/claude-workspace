"""Tests for cw._sse_util: guarded SSE send helper."""

from __future__ import annotations

from typing import Any

import pytest

anyio = pytest.importorskip(
    "anyio", reason="requires mcp extras: pip install 'cw[mcp]'"
)

from cw._sse_util import _send_or_close


class TestSendOrClose:
    def test_success_path(self) -> None:
        async def _run() -> tuple[bool, str]:
            send, recv = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            ok = await _send_or_close(send, "hello")
            received = await recv.receive()
            return ok, received

        ok, received = anyio.run(_run)
        assert ok is True
        assert received == "hello"

    def test_closed_resource_error_path(self) -> None:
        async def _run() -> bool:
            send, _recv = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            await send.aclose()
            return await _send_or_close(send, "hello")

        assert anyio.run(_run) is False

    def test_broken_resource_error_path(self) -> None:
        async def _run() -> bool:
            send, recv = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            await recv.aclose()
            return await _send_or_close(send, "hello")

        assert anyio.run(_run) is False
