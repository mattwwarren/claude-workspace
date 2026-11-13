"""Opt-in live checks for the codex session probe (RFC 0018 D1, #3064).

Never runs in default CI: the module is ``integration``-marked, and each test
self-skips unless its gate is set. ``nightly-codex.yml`` sets only
``INTEGRATION_CODEX_LIVE``, so the probe stays skipped there.

* create and resume: ``INTEGRATION_CODEX_LIVE=1``,
  ``CW_CODEX_LIVE_SESSION_SMOKE=1`` and ``CW_CODEX_LIVE_SESSION_MODEL=<model>``.
* MCP loading: ``INTEGRATION_CODEX_LIVE=1`` and ``CW_TRACKER_IO_LIVE_SERVER``,
  reusing the tracker-IO probe by import (underscore prevents double collection).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_tracker_io_live import (
    test_turn_argv_loads_the_server_with_only_the_allowlist as _mcp_probe,
)

pytestmark = pytest.mark.integration

_CODEX_LIVE = os.environ.get("INTEGRATION_CODEX_LIVE", "").strip() not in ("", "0")
_SMOKE = os.environ.get("CW_CODEX_LIVE_SESSION_SMOKE", "").strip() == "1"
_MODEL = os.environ.get("CW_CODEX_LIVE_SESSION_MODEL", "").strip()
_SERVER = os.environ.get("CW_TRACKER_IO_LIVE_SERVER", "").strip()
_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "probe_codex_live_session.py"
)


@pytest.mark.skipif(
    not (_CODEX_LIVE and _SMOKE and _MODEL),
    reason=(
        "set INTEGRATION_CODEX_LIVE=1, CW_CODEX_LIVE_SESSION_SMOKE=1 and"
        " CW_CODEX_LIVE_SESSION_MODEL"
    ),
)
def test_create_resume_probe_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--model", _MODEL],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, "probe must print exactly one JSON line"
    result = json.loads(lines[0])
    assert result["status"] == "passed", result
    assert result["create"]["exit_code"] == 0
    assert result["resume"]["exit_code"] == 0
    assert result["resume"]["id_matches"] is True
    assert result["error_code"] is None
    assert proc.stderr == ""


@pytest.mark.skipif(
    not (_CODEX_LIVE and _SERVER),
    reason="set INTEGRATION_CODEX_LIVE=1 and CW_TRACKER_IO_LIVE_SERVER",
)
def test_mcp_server_loads_headless() -> None:
    _mcp_probe()
