"""Shared builders for composed opencode JSONL session logs (GitHub #2490).

FIXTURE PROVENANCE -- read before trusting any log built here.

opencode's ``--format json`` log is an external system's output, so a fixture
for it should be a redacted capture, not an invention. No capture of the failing
session exists. What IS observed is only what
``tests/test_opencode_contract_live.py`` (``INTEGRATION_OPENCODE_LIVE``, nightly)
asserts against a real run: that some event has ``type == "step_finish"``, and
that the ``text`` events' ``part.text`` strings concatenate (via
``extract_text_from_jsonl``) to non-empty text.

Everything else here is COMPOSED, not observed -- hand-written, or inherited
from earlier tests that were themselves hand-written:

* the ``part`` object on every event, the ``step_start`` event and its
  ``{"type": "step-start"}`` part, and the ``step_finish`` ``reason`` values
  (``"stop"``): nothing pins their presence or spelling;
* the arrangement -- an earlier stage's sentinel quoted in one ``text`` event,
  the final sentinel in a later one;
* a bare stderr line merged into the stream and a ``text`` event whose ``part``
  is ``null``. These are HYPOTHESIZED robustness cases: they exercise the
  defensive branches of ``opencode_runner._iter_text_events``, they are not
  observed output.
* the ``invalid_merge_gate_frame`` sentinel (#2629): a COMPOSED frame built
  from ``make_blocked`` with the one invalid field set, not a capture of the
  failing session.

The first real operator capture of an affected session (or a live-contract run)
should replace the composed arrangement wholesale; until then a green test here
proves the parser's handling of the composed shape, not of a real log.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.opencode_runner import OPENCODE_LOG_RELATIVE_PATH, make_blocked

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult

# One log line: a JSON event object, or a raw (non-JSON) line.
LogLine = dict[str, object] | str


def framed(result: AutoDevResult) -> str:
    """*result* wrapped in the ``AUTO_DEV_RESULT`` open/close sentinel frame."""
    return f"<<<AUTO_DEV_RESULT\n{result.model_dump_json()}\nAUTO_DEV_RESULT>>>"


def invalid_merge_gate_frame(worktree: Path, *, ticket_id: str = "T-1") -> str:
    """A framed ``merge_gate_blocked`` result whose ``pr`` is set (#2629).

    ``merge_gate_blocked`` requires ``pr`` to be null, so the parser rejects the
    frame as ``validation_failed``. The ``ticket_id`` must match the task's, or
    the parser discards the frame before validating it.
    """
    payload = make_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason="agent_block",
        stage_reached="stage4a_merge_gate",
    ).model_dump(mode="json")
    payload["status"] = "merge_gate_blocked"
    payload["pr"] = {"number": 0, "url": "", "auto_merge": False, "base": "main"}
    return f"<<<AUTO_DEV_RESULT\n{json.dumps(payload)}\nAUTO_DEV_RESULT>>>"


def text_event(text: str) -> dict[str, object]:
    """An opencode ``text`` event carrying *text*."""
    return {"type": "text", "part": {"text": text}}


def earlier_stage_then_final_log(
    earlier: AutoDevResult, final: AutoDevResult
) -> list[LogLine]:
    """A session log quoting *earlier*'s sentinel, then ending with *final*'s."""
    return [
        {"type": "step_start", "part": {"type": "step-start"}},
        text_event(f"Prior leg reported:\n{framed(earlier)}\nNow finalizing."),
        {"type": "step_finish", "part": {"reason": "stop"}},
        "warn: stderr line merged into the json stream",
        {"type": "text", "part": None},
        {"type": "step_start", "part": {"type": "step-start"}},
        text_event(framed(final)),
        {"type": "step_finish", "part": {"reason": "stop"}},
    ]


def log_content(lines: list[LogLine]) -> str:
    """Render *lines* as JSONL text (raw string lines pass through verbatim)."""
    return (
        "\n".join(ln if isinstance(ln, str) else json.dumps(ln) for ln in lines) + "\n"
    )


def write_opencode_log(worktree: Path, lines: list[LogLine]) -> Path:
    """Write *lines* to the worktree's ``.cw/opencode.log``; return its path."""
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log_content(lines), encoding="utf-8")
    return log_path
