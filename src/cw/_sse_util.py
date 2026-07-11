"""Shared helper for guarded SSE writes to a peer memory stream.

Isolated from :mod:`cw._util` because that module must stay ``anyio``-free so
:mod:`cw.cli` and :mod:`cw.reconcile` load at base install (``anyio`` is only
available with the ``[mcp]`` extra). This module is imported solely by the SSE
event-server ``_drain`` closures, which already require the extra.
"""

from __future__ import annotations

from typing import Any


async def _send_or_close(write_stream: Any, session_msg: Any) -> bool:
    """Send *session_msg* on *write_stream*, tolerating a gone peer.

    Returns ``True`` on success, ``False`` when the peer stream has already
    closed (``ClosedResourceError``/``BrokenResourceError``) — signalling the
    caller to stop draining rather than crash on an idle-disconnect race.
    """
    import anyio

    try:
        await write_stream.send(session_msg)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        return False
    return True
