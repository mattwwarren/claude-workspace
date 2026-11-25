"""The saved REVIEW-park blocking findings a regressed IMPL run is handed (#3762).

A claude-native REVIEW park posts its still-blocking findings as a
``## Blocking Review Findings`` tracker comment, and no verdict-shaped artifact
survives it. ``cw post-comment`` therefore saves the redacted body of that
comment, from a RUNNING REVIEW row, to ``.cw/review-park-findings.json`` in the
session worktree; the regress findings rule
(``cw.stage_verification.impl_regress_findings``) reads it back when the row
regresses into IMPL. Every successful
``cw review workspace init`` clears it, so a new review attempt never inherits
an older attempt's park text.

Logging rule: ``ticket_id`` is the only identity field. Findings text and
absolute worktree paths are never logged, and an exception is logged by class
name only, because ``OSError.__str__`` embeds the absolute path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cw.atomic import atomic_write_text
from cw.models.review_feedback import MAX_REVIEW_REPORT_BYTES
from cw.stage_verification.posted_text import redact_and_cap_bytes

_log = logging.getLogger(__name__)

REVIEW_PARK_FINDINGS_RELATIVE_PATH: Final = Path(".cw") / "review-park-findings.json"

#: Refusal strings equal the ``WithheldReason`` values of the regress rule; they
#: are repeated here (not imported) so this module never imports the rule.
_ARTIFACT_MISSING: Final = "artifact_missing"
_NOT_OWNED: Final = "not_owned"


class ReviewParkFindings(BaseModel):
    """The saved park body, stamped with its ticket and the head at post time."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    reviewed_sha: str
    text: str = Field(min_length=1)


class ParkFindingsRead(NamedTuple):
    """The saved findings, or why none can be delivered. Exactly one is set."""

    findings: ReviewParkFindings | None
    refusal: Literal["artifact_missing", "not_owned"] | None


def write_park_findings(
    worktree: Path, *, ticket_id: str, reviewed_sha: str, text: str
) -> bool:
    """Save *text* (redacted and capped) for *ticket_id*; ``False`` when not saved.

    Never raises: a blank text or an ``OSError`` returns ``False``.
    """
    capped = redact_and_cap_bytes(text, cap_bytes=MAX_REVIEW_REPORT_BYTES)
    if not capped.strip():
        _log.debug(
            "review_park_findings_skipped ticket=%s reason=blank_text", ticket_id
        )
        return False
    record = ReviewParkFindings(
        ticket_id=ticket_id, reviewed_sha=reviewed_sha, text=capped
    )
    path = worktree / REVIEW_PARK_FINDINGS_RELATIVE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, record.model_dump_json() + "\n")
    except OSError as exc:
        _log.warning(
            "review_park_findings_write_failed ticket=%s error=%s",
            ticket_id,
            type(exc).__name__,
        )
        return False
    _log.info(
        "review_park_findings_saved ticket=%s bytes=%d",
        ticket_id,
        len(capped.encode("utf-8")),
    )
    return True


def read_park_findings(worktree: Path, ticket_id: str) -> ParkFindingsRead:
    """Read the saved findings for *ticket_id*.

    An absent, unreadable, invalid or blank file is ``artifact_missing``; a file
    stamped for another ticket is ``not_owned``.
    """
    path = worktree / REVIEW_PARK_FINDINGS_RELATIVE_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ParkFindingsRead(None, _ARTIFACT_MISSING)
    except (OSError, UnicodeDecodeError) as exc:
        return _unreadable(ticket_id, exc)
    try:
        findings = ReviewParkFindings.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        return _unreadable(ticket_id, exc)
    if not findings.text.strip():
        return _unreadable(ticket_id, ValueError("blank"))
    if findings.ticket_id != ticket_id:
        return ParkFindingsRead(None, _NOT_OWNED)
    return ParkFindingsRead(findings, None)


def _unreadable(ticket_id: str, exc: Exception) -> ParkFindingsRead:
    _log.debug(
        "review_park_findings_unreadable ticket=%s error=%s",
        ticket_id,
        type(exc).__name__,
    )
    return ParkFindingsRead(None, _ARTIFACT_MISSING)


def clear_park_findings(worktree: Path, *, ticket_id: str) -> None:
    """Delete the saved findings. A missing file is a no-op; never raises."""
    path = worktree / REVIEW_PARK_FINDINGS_RELATIVE_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log.warning(
            "review_park_findings_clear_failed ticket=%s error=%s",
            ticket_id,
            type(exc).__name__,
        )
        return
    _log.debug("review_park_findings_cleared ticket=%s", ticket_id)
