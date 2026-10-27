"""Doc guard: the cw-smoke-test Step 3 poll survives an empty ``event tail``.

#1119 comment: ``cw event tail --json`` prints the literal ``[]`` when no event
matches yet (``_emit_tail_events``) and one JSON object per line otherwise. The
skill's ``jq`` filter indexed ``.payload`` directly, so the first empty poll --
the normal state while the worker is still running -- exited 5 with
``Cannot index array with string "payload"``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from cw.models.enums import OrchestratorEventType
from cw.models.events import OrchestratorEvent
from tests.conftest import _REPO_ROOT

_SKILL = _REPO_ROOT / ".claude" / "skills" / "cw-smoke-test" / "SKILL.md"
_WORKER_ID = "probe-session"
_JQ_FILTER = re.compile(r"jq -r '(?P<filter>[^']*<WORKER_ID>[^']*)'")

# Skip only off CI: a CI runner without jq must fail, not silently pass unrun.
requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not installed",
)


def _poll_filter() -> str:
    """Return the Step 3 completion-poll ``jq`` filter with the worker id filled in."""
    match = _JQ_FILTER.search(_SKILL.read_text(encoding="utf-8"))
    assert match is not None, "Step 3 poll no longer pipes event tail into jq"
    return match.group("filter").replace("<WORKER_ID>", _WORKER_ID)


def _run_jq(stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jq", "-r", _poll_filter()],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _completed_event(session_id: str) -> str:
    return OrchestratorEvent(
        type=OrchestratorEventType.SESSION_COMPLETED,
        payload={"session_id": session_id},
    ).model_dump_json()


@requires_jq
def test_poll_filter_exits_zero_on_empty_tail() -> None:
    result = _run_jq("[]\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@requires_jq
def test_poll_filter_returns_row_for_matching_worker() -> None:
    result = _run_jq(_completed_event(_WORKER_ID) + "\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["payload"]["session_id"] == _WORKER_ID


@requires_jq
def test_poll_filter_ignores_other_workers() -> None:
    result = _run_jq(_completed_event("someone-else") + "\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@requires_jq
def test_poll_filter_finds_match_after_other_events() -> None:
    stream = _completed_event("someone-else") + "\n" + _completed_event(_WORKER_ID)
    result = _run_jq(stream + "\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["payload"]["session_id"] == _WORKER_ID
