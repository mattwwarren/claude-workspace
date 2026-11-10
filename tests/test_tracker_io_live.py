"""Opt-in live probes for the tracker-IO turn (#3057, RFC 0018 A2).

Never runs in default CI: the file is ``integration``-marked (excluded by
``-m "not integration"``) and every test self-skips unless its env gates are
set. ``nightly-codex.yml`` sets only ``INTEGRATION_CODEX_LIVE``, so these skip
there too.

Gates:

* read-only probe: ``INTEGRATION_CODEX_LIVE=1`` and
  ``CW_TRACKER_IO_LIVE_SERVER=<the codex MCP server name for the tracker>``.
  It runs the real turn argv and asks the agent which of the server's tools it
  can call, without calling any. That proves codex loads the MCP server
  headless and that ``enabled_tools`` hides every non-allowlisted tool.
* marker probe (it WRITES one comment): additionally
  ``CW_TRACKER_IO_LIVE_TICKET=<ticket id>`` and ``CW_TRACKER_IO_LIVE_WRITE=1``.
  It posts one marker-bearing part through ``post_parts_via_turn`` and, when
  ``CW_TRACKER_IO_PROBE_OUT`` is set, writes the redacted capture there. The
  operator commits it as ``tests/fixtures/tracker_io/linear_marker_probe.json``
  and updates the runbook record (see ``tests/test_tracker_io_marker_probe.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.codex_runner import CodexRunResult, RealCodexRunner
from cw.config import state_dir
from cw.models import ClientConfig
from cw.models.client import TrackerMcpGateConfig
from cw.tracker import TRACKER_LINEAR
from cw.tracker_io import (
    TrackerIoContext,
    build_tracker_io_argv,
    comment_tools_for,
    post_parts_via_turn,
)
from tests.test_tracker_io_marker_probe import MarkerProbeCapture

pytestmark = pytest.mark.integration

_CODEX_LIVE = os.environ.get("INTEGRATION_CODEX_LIVE", "").strip() not in ("", "0")
_SERVER = os.environ.get("CW_TRACKER_IO_LIVE_SERVER", "").strip()
_TICKET = os.environ.get("CW_TRACKER_IO_LIVE_TICKET", "").strip()
_WRITE = os.environ.get("CW_TRACKER_IO_LIVE_WRITE", "").strip() == "1"
_PROBE_OUT = os.environ.get("CW_TRACKER_IO_PROBE_OUT", "").strip()
_TRACKER = os.environ.get("CW_TRACKER_IO_LIVE_TRACKER", TRACKER_LINEAR).strip()

_READ_GATE = pytest.mark.skipif(
    not (_CODEX_LIVE and _SERVER),
    reason="set INTEGRATION_CODEX_LIVE=1 and CW_TRACKER_IO_LIVE_SERVER",
)
_WRITE_GATE = pytest.mark.skipif(
    not (_CODEX_LIVE and _SERVER and _TICKET and _WRITE),
    reason=(
        "set INTEGRATION_CODEX_LIVE=1, CW_TRACKER_IO_LIVE_SERVER,"
        " CW_TRACKER_IO_LIVE_TICKET and CW_TRACKER_IO_LIVE_WRITE=1"
    ),
)
_TIMEOUT_SECONDS = 300
_TOOLS_SCHEMA = {
    "type": "object",
    "properties": {"tools": {"type": "array", "items": {"type": "string"}}},
    "required": ["tools"],
    "additionalProperties": False,
}
_TOOLS_PROMPT = (
    "Do not call any tool. List the exact name of every tool of the MCP server"
    " `{server}` that you are able to call in this session, and reply with only"
    ' the JSON {{"tools": [...]}} the output schema describes.'
)


class _CapturingRunner:
    """``RealCodexRunner``, keeping the raw result for the capture."""

    def __init__(self) -> None:
        self.raw: str | None = None

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        result = RealCodexRunner().run(worktree, argv, timeout_seconds, stdin=stdin)
        self.raw = result.output_file_content
        return result


def _codex_version() -> str:
    proc = subprocess.run(
        ["codex", "--version"], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip()


def _redact(text: str | None) -> str | None:
    if text is None:
        return None
    return text.replace(_TICKET, "<ticket>").replace(_SERVER, "<server>")


@_READ_GATE
def test_turn_argv_loads_the_server_with_only_the_allowlist() -> None:
    scratch = state_dir() / "codex-review" / f"probe-{uuid.uuid4().hex}"
    scratch.mkdir(parents=True)
    try:
        schema = scratch / "schema.json"
        schema.write_text(json.dumps(_TOOLS_SCHEMA), encoding="utf-8")
        out = scratch / "result.json"
        argv = build_tracker_io_argv(
            server=_SERVER,
            tracker=_TRACKER,
            schema_path=schema,
            output_path=out,
            other_servers=(),
        )
        result = RealCodexRunner().run(
            scratch,
            argv,
            _TIMEOUT_SECONDS,
            stdin=_TOOLS_PROMPT.format(server=_SERVER),
        )
    finally:
        content = None
        if (scratch / "result.json").exists():
            content = (scratch / "result.json").read_text(encoding="utf-8")
        shutil.rmtree(scratch, ignore_errors=True)
    assert result.returncode == 0, result.stderr
    assert content is not None, "codex wrote no result"
    tools = {
        str(t).rsplit(".", 1)[-1].rsplit("__", 1)[-1]
        for t in json.loads(content)["tools"]
    }
    assert set(comment_tools_for(_TRACKER)) <= tools, tools
    assert "get_issue" not in tools, tools


@_WRITE_GATE
def test_marker_probe_posts_one_part_and_records_survival(tmp_path: Path) -> None:
    runner = _CapturingRunner()
    client = ClientConfig(
        name="tracker-io-probe",
        workspace_path=tmp_path,
        default_branch="main",
        tracker_mcp_gate=TrackerMcpGateConfig(enabled=True, servers={"codex": _SERVER}),
    )
    context = TrackerIoContext(runner=runner, client=client, worktree=tmp_path)
    text = (
        "cw tracker-io marker probe (#3057, RFC 0018 A2). This comment tests"
        " whether a hidden HTML marker survives; it is safe to delete."
    )
    started = time.monotonic()
    outcome = post_parts_via_turn(
        _TICKET, text, tracker=_TRACKER, context=context, session_id="live-probe"
    )
    seconds = time.monotonic() - started
    capture = MarkerProbeCapture(
        probe="linear_marker",
        recorded_at=datetime.now(UTC).isoformat(),
        codex_version=_codex_version(),
        survived=outcome.marker_survived,
        verified=outcome.verified,
        reason=None if outcome.reason is None else outcome.reason.value,
        public_state=outcome.public_state.value,
        seconds=round(seconds, 1),
        raw_result=_redact(runner.raw),
    )
    if _PROBE_OUT:
        probe_path = Path(_PROBE_OUT)
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        probe_path.write_text(
            capture.model_dump_json(indent=1) + "\n", encoding="utf-8"
        )
    assert outcome.verified, outcome
