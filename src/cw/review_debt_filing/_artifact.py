"""Read deferrals from, and write tickets back to, the artifact (#3041, R14).

The ``.cw/deferred-findings.md`` artifact is what Stage 4 copies into the PR
body and Step H3 harvests. Once a deferral has a ticket, its entry gains the
optional ``ticket:`` line, so H3 skips it. :func:`split_fixture_entries`
screens out entries a test leaked into an artifact, before filing (#3113).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.exceptions import CwError
from cw.review_adjudication import (
    read_deferred_findings_md,
    render_deferred_findings_md,
    with_tickets,
)
from cw.review_adjudication._models import DEFERRED_OUTCOME
from cw.review_debt_filing._models import DebtEntry

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.models.review_debt_record import ReviewDebtRecord
    from cw.review_adjudication import Adjudication


#: Paths only the test suite's fixture findings name
#: (``tests/conftest.py::_finding_kwargs``). No real file lives there, so an
#: unticketed entry naming one leaked from a test into an artifact (#3113).
FIXTURE_ENTRY_PATHS = frozenset({"src/cw/foo.py"})


def _normalised(file: str) -> str:
    text = file.strip().removeprefix("./")
    return str(PurePosixPath(text)) if text else text


def is_fixture_entry(entry: DebtEntry) -> bool:
    """True for an unticketed entry naming a :data:`FIXTURE_ENTRY_PATHS` path."""
    return not entry.ticket and _normalised(entry.file) in FIXTURE_ENTRY_PATHS


def split_fixture_entries(
    entries: Sequence[DebtEntry],
) -> tuple[list[DebtEntry], list[DebtEntry]]:
    """``(fileable, refused)``: *entries* split by :func:`is_fixture_entry`."""
    fileable: list[DebtEntry] = []
    refused: list[DebtEntry] = []
    for entry in entries:
        (refused if is_fixture_entry(entry) else fileable).append(entry)
    return fileable, refused


def debt_entry(adjudication: Adjudication) -> DebtEntry:
    """The :class:`DebtEntry` for one ``defer`` adjudication."""
    return DebtEntry(
        severity=adjudication.severity,
        file=adjudication.file,
        summary=adjudication.summary,
        rationale=adjudication.rationale,
        line=adjudication.line_start,
        evidence=adjudication.evidence,
        rule_id=adjudication.rule_id or "",
        fingerprint=adjudication.fingerprint,
        ticket=adjudication.ticket,
    )


def read_artifact(path: Path) -> list[Adjudication]:
    """Every entry in the artifact at *path*; ``[]`` when it does not exist.

    Raises:
        CwError: when the artifact exists but does not parse; filing refuses
            to guess rather than overwrite records it cannot read.
    """
    try:
        return read_deferred_findings_md(path)
    except (OSError, ValueError) as exc:
        msg = f"Could not read the DEFERRED-REVIEW-FINDINGS artifact at {path}: {exc}"
        raise CwError(msg) from exc


def artifact_debt_entries(path: Path) -> list[DebtEntry]:
    """The artifact's ``defer`` entries as :class:`DebtEntry` values."""
    return [debt_entry(a) for a in read_artifact(path) if a.outcome == DEFERRED_OUTCOME]


def ticket_value(record: ReviewDebtRecord) -> str | None:
    """What a ``ticket:`` line records for *record*: its URL, else its ID."""
    return record.ticket_url or record.ticket_id


def write_artifact_tickets(path: Path, records: Sequence[ReviewDebtRecord]) -> bool:
    """Fill in ``ticket:`` for every artifact deferral *records* tickets.

    Returns True when the file changed. A missing artifact is left missing.
    """
    entries = read_artifact(path)
    by_key = {
        record.key: value
        for record in records
        if (value := ticket_value(record)) is not None
    }
    tickets: dict[tuple[str, str, str], str] = {}
    for entry in entries:
        if entry.outcome != DEFERRED_OUTCOME:
            continue
        value = by_key.get(debt_entry(entry).key)
        if value is not None:
            tickets[(entry.file, entry.summary, entry.fingerprint or "")] = value
    filled = with_tickets(entries, tickets)
    if filled == entries:
        return False
    atomic_write_text(path, render_deferred_findings_md(filled))
    return True
