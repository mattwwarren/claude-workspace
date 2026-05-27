"""Native daemon smoke tests — require a real claude CLI with auth.

These tests call ``claude --bg`` against the real Anthropic API. They are
excluded from PR CI and the unit test matrix. The nightly native workflow
opts them in by setting ``INTEGRATION_REAL_API=1``.

Run manually::

    ANTHROPIC_API_KEY=<key> INTEGRATION_REAL_API=1 \\
        uv run pytest tests/test_native_smoke.py -v -m integration
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cw.native_daemon import SHORT_SESSION_ID_RE, RealNativeDaemonClient

pytestmark = pytest.mark.integration

_REAL_API = os.environ.get("INTEGRATION_REAL_API", "").strip() not in ("", "0")


@pytest.mark.skipif(not _REAL_API, reason="INTEGRATION_REAL_API not set")
class TestNativeDaemonSmoke:
    """End-to-end against the real claude --bg daemon.

    Each test stops the spawned session in a finally block so the nightly
    runner does not accumulate orphaned sessions on assertion failures.
    """

    def test_spawn_returns_short_id(self, tmp_path: Path) -> None:
        client = RealNativeDaemonClient()
        short_id = client.spawn_bg(cwd=tmp_path, prompt="/version")
        try:
            assert SHORT_SESSION_ID_RE.match(short_id), (
                f"Expected 8-hex short id, got {short_id!r}"
            )
        finally:
            client.stop(short_id)

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        """Stopping an already-stopped session must not raise."""
        client = RealNativeDaemonClient()
        short_id = client.spawn_bg(cwd=tmp_path, prompt="/version")
        client.stop(short_id)
        # Second stop should silently succeed (claude stop is best-effort).
        client.stop(short_id)
