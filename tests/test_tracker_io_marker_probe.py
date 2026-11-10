"""The runbook's Linear marker record agrees with the probe capture (#3057, R3).

Whether Linear keeps the hidden ``<!-- cw-agent-authored -->`` marker is only
known from the opt-in live probe in ``tests/test_tracker_io_live.py``. Its
capture, ``tests/fixtures/tracker_io/linear_marker_probe.json``, is an
operator-run artifact committed outside the ticket that shipped the probe.
Until it exists the runbook must say UNVERIFIED and name the fallback; once it
exists the runbook must state the same ``survived`` value the capture holds.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

_REPO = Path(__file__).resolve().parents[1]
CAPTURE_PATH = _REPO / "tests" / "fixtures" / "tracker_io" / "linear_marker_probe.json"
_RUNBOOK = _REPO / "docs" / "dispatch-runbook.md"
_STATUS_RE = re.compile(
    r"Linear marker survival: \*\*(UNVERIFIED|VERIFIED \(survived=(?:true|false)\))\*\*"
)
_UNVERIFIED = "UNVERIFIED"
# The documented fallback when markers are stripped: the self-disclosure
# phrase plus author identity (cw.gh.issues._SELF_DISCLOSURE_RE).
_FALLBACK_NEEDLE = "_SELF_DISCLOSURE_RE"


class MarkerProbeCapture(BaseModel):
    """What the live marker probe writes to ``CW_TRACKER_IO_PROBE_OUT``."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    probe: Literal["linear_marker"]
    recorded_at: str
    codex_version: str
    survived: bool | None
    verified: bool
    reason: str | None
    public_state: str
    seconds: float
    raw_result: str | None


def marker_record_problems(capture_path: Path, runbook: str) -> list[str]:
    """Every disagreement between the runbook record and the capture."""
    statuses = _STATUS_RE.findall(runbook)
    if len(statuses) != 1:
        return [f"the runbook states the marker record {len(statuses)} times, not 1"]
    (status,) = statuses
    if not capture_path.exists():
        problems = []
        if status != _UNVERIFIED:
            problems.append(f"the runbook says {status} but no capture exists")
        if _FALLBACK_NEEDLE not in runbook:
            problems.append(
                f"the runbook does not name the {_FALLBACK_NEEDLE} fallback"
            )
        return problems
    capture = MarkerProbeCapture.model_validate_json(
        capture_path.read_text(encoding="utf-8")
    )
    want = f"VERIFIED (survived={str(capture.survived).lower()})"
    return [] if status == want else [f"the runbook says {status}; capture: {want}"]


def _capture(survived: bool) -> str:
    return MarkerProbeCapture(
        probe="linear_marker",
        recorded_at="2026-11-10T00:00:00+00:00",
        codex_version="codex-cli 0.167.0",
        survived=survived,
        verified=True,
        reason=None,
        public_state="confirmed",
        seconds=12.5,
        raw_result="{}",
    ).model_dump_json()


def _runbook(status: str) -> str:
    return f"Linear marker survival: **{status}**. Fallback: {_FALLBACK_NEEDLE}.\n"


def test_repo_runbook_agrees_with_the_repo_capture() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    assert marker_record_problems(CAPTURE_PATH, runbook) == []


def test_repo_state_is_unverified_while_the_capture_is_absent() -> None:
    if CAPTURE_PATH.exists():
        return
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    assert _STATUS_RE.findall(runbook) == [_UNVERIFIED]


def test_absent_capture_requires_unverified(tmp_path: Path) -> None:
    absent = tmp_path / "none.json"
    assert marker_record_problems(absent, _runbook(_UNVERIFIED)) == []
    (problem,) = marker_record_problems(absent, _runbook("VERIFIED (survived=true)"))
    assert "no capture exists" in problem


def test_absent_capture_requires_the_fallback(tmp_path: Path) -> None:
    runbook = "Linear marker survival: **UNVERIFIED**.\n"
    (problem,) = marker_record_problems(tmp_path / "none.json", runbook)
    assert _FALLBACK_NEEDLE in problem


def test_present_capture_must_match(tmp_path: Path) -> None:
    capture = tmp_path / "probe.json"
    capture.write_text(_capture(survived=False), encoding="utf-8")
    assert marker_record_problems(capture, _runbook("VERIFIED (survived=false)")) == []
    assert marker_record_problems(capture, _runbook("VERIFIED (survived=true)"))
    assert marker_record_problems(capture, _runbook(_UNVERIFIED))


def test_record_must_appear_exactly_once(tmp_path: Path) -> None:
    twice = _runbook(_UNVERIFIED) * 2
    (problem,) = marker_record_problems(tmp_path / "none.json", twice)
    assert "2 times" in problem
    assert marker_record_problems(tmp_path / "none.json", "nothing")
