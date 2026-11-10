"""Doc guards for the session-id derivation (#2651).

The stage-event fences must not hand-write ``session_id`` from ``$CW_SESSION``
(``cw event record`` derives it from cw-context.json), the conflicted-files
path must not key on ``$CW_SESSION``, and the review fix loop must record its
handoff through ``cw dev-queue record-fix-handoff`` rather than an inline
writer.

The stage-invocation scan matches the *bare* substrings ``session_id`` and
``CW_SESSION``, never the quoted form: inside ``--payload "..."`` the quotes
are backslash-escaped (``\\"session_id\\":\\"$CW_SESSION\\"``), so the literal
``"session_id"`` is never a substring of the markdown and a check on it would
pass vacuously.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_stage_event_no_swallow import _stage_event_invocations

_ROOT = Path(__file__).resolve().parents[1]
_COMMANDS = sorted((_ROOT / ".claude" / "commands").glob("auto-dev*.md"))

# Floor on the scanned stage invocations (19 today): a broken scan that finds
# nothing must not pass the "no invocation names a session" guard.
_MIN_STAGE_INVOCATIONS = 15

_TICKET_FRAGMENT = '\\"ticket_id\\":\\"$TICKET\\"'

# Narrow needles: the legitimate ``$CW_SESSION`` warning prose in
# ``auto-dev-impl.md`` ("never key a path on it") must stay allowed.
_FORBIDDEN = (
    "requested_by_session_id='$CW_SESSION'",
    "conflicted-files-$CW_SESSION",
    "CW_SESSION=$(",
)


def _names_session(invocation: str) -> bool:
    """True when a stage-event invocation names a session id in any spelling."""
    return "session_id" in invocation or "CW_SESSION" in invocation


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_invocations() -> list[tuple[str, str]]:
    return [
        (name, invocation)
        for name, invocations in _stage_event_invocations().items()
        for invocation in invocations
    ]


@pytest.mark.parametrize(
    ("invocation", "expected"),
    [
        (
            '--payload "{\\"session_id\\":\\"$CW_SESSION\\",'
            '\\"ticket_id\\":\\"$TICKET\\"}"',
            True,
        ),
        (
            '--payload "{\\"session_id\\":\\"abc\\",\\"ticket_id\\":\\"$TICKET\\"}"',
            True,
        ),
        ('--payload \'{"session_id":"abc"}\'', True),
        ('--payload "{\\"x\\":\\"$CW_SESSION\\"}"', True),
        (
            '--payload "{\\"ticket_id\\":\\"$TICKET\\",\\"stage\\":\\"s0_intake\\"}"',
            False,
        ),
    ],
)
def test_names_session_predicate_catches_every_spelling(
    invocation: str, *, expected: bool
) -> None:
    """The guard's predicate flags a half-removal, not just the exact old text."""
    assert _names_session(invocation) is expected


def test_no_stage_event_invocation_names_a_session() -> None:
    invocations = _all_invocations()
    assert len(invocations) >= _MIN_STAGE_INVOCATIONS, len(invocations)
    offenders = [
        f"{name}: {invocation.splitlines()[0].strip()}"
        for name, invocation in invocations
        if _names_session(invocation)
    ]
    assert offenders == []


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
    """Dropping session_id must not drop the ticket from any stage payload."""
    invocations = _all_invocations()
    assert len(invocations) >= _MIN_STAGE_INVOCATIONS, len(invocations)
    missing = [
        f"{name}: {invocation.splitlines()[0].strip()}"
        for name, invocation in invocations
        if _TICKET_FRAGMENT not in invocation
    ]
    assert missing == []
