"""Doc guards for the session-id derivation (#2651).

The stage-event fences must not hand-write ``session_id`` from ``$CW_SESSION``
(``cw event record`` derives it from cw-context.json), the conflicted-files
path must not key on ``$CW_SESSION``, and the review fix loop must record its
handoff through ``cw dev-queue record-fix-handoff`` rather than an inline
writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_COMMANDS = sorted((_ROOT / ".claude" / "commands").glob("auto-dev*.md"))

_FORBIDDEN = (
    r"session_id\":\"$CW_SESSION",
    "requested_by_session_id='$CW_SESSION'",
    "conflicted-files-$CW_SESSION",
    "CW_SESSION=$(",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("needle", _FORBIDDEN)
def test_no_command_doc_interpolates_cw_session(needle: str) -> None:
    hits = [p.name for p in _COMMANDS if needle in _text(p)]
    assert hits == [], f"{needle!r} still in: {hits}"


def test_review_doc_records_handoff_through_the_command() -> None:
    text = _text(_ROOT / ".claude" / "commands" / "auto-dev-review.md")
    assert "cw dev-queue record-fix-handoff" in text
    assert "--prompt-file .cw/fix-agent-prompt.md" in text
    assert "PendingFixDispatch(" not in text


def test_stage_payloads_still_name_ticket() -> None:
    """Dropping session_id must not drop the ticket from the stage payloads."""
    text = _text(_ROOT / ".claude" / "commands" / "auto-dev-plan.md")
    assert '\\"ticket_id\\":\\"$TICKET\\"' in text
