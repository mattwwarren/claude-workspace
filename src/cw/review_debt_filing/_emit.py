"""Emit one redacted payload file per unticketed deferral (#3041, R9/R10).

Used for an MCP tracker (Linear: the worker files each payload through MCP and
reports the IDs with ``cw review debt record``) and for a GitHub client with
``auto_create_issues: false`` (Step H3 or the operator files them). The first
write wins: a payload file whose ``key`` already matches is never rewritten,
so a re-emit is byte-identical and the worker can search for the marker before
creating. Emit never deletes a file.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple

from cw.atomic import atomic_write_text
from cw.models.review_debt_record import EMITTED_MODE, ReviewDebtRecord
from cw.review_debt_filing._events import emit_emitted
from cw.review_debt_filing._file import unticketed
from cw.review_debt_filing._render import render_payload

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.review_debt_filing._events import AuditWarning
    from cw.review_debt_filing._models import DebtEntry, FilingContext

#: ``review.debt_emitted`` ``cause`` values.
CAUSE_MCP = "mcp"
CAUSE_AUTO_CREATE_OFF = "auto_create_issues_false"


class EmittedFile(NamedTuple):
    """One payload file for one unticketed entry."""

    key: str
    path: Path
    written: bool


class EmitResult(NamedTuple):
    """What :func:`emit_review_debt` did.

    ``refused`` is True when the repo visibility was unknown and nothing was
    written. ``records`` are the ``emitted`` records for the caller to upsert.
    """

    refused: bool
    files: list[EmittedFile]
    records: list[ReviewDebtRecord]
    audit_warnings: tuple[AuditWarning, ...] = ()


def payload_path(out_dir: Path, key: str) -> Path:
    """``<out_dir>/<key with ':' as '_'>.json``; injective over debt keys."""
    return out_dir / f"{key.replace(':', '_')}.json"


def _already_written(path: Path, key: str) -> bool:
    """True when *path* is already the payload for *key*."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(existing, dict) and existing.get("key") == key


def _payload_text(entry: DebtEntry, ctx: FilingContext) -> str:
    payload = render_payload(entry, ctx)
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def emit_review_debt(
    entries: Sequence[DebtEntry],
    ctx: FilingContext,
    out_dir: Path,
    *,
    cause: str,
) -> EmitResult:
    """Write a payload for every unticketed entry the row has no ticket for."""
    ticketed = {
        record.key for record in ctx.existing or () if record.ticket_id is not None
    }
    pending = sorted(
        (entry for entry in unticketed(entries) if entry.key not in ticketed),
        key=lambda entry: entry.key,
    )
    if pending and ctx.visibility is None:
        return EmitResult(refused=True, files=[], records=[])
    recorded = {record.key for record in ctx.existing or ()}
    files: list[EmittedFile] = []
    records: list[ReviewDebtRecord] = []
    audit_warnings: list[AuditWarning] = []
    if pending:
        out_dir.mkdir(parents=True, exist_ok=True)
    for entry in pending:
        path = payload_path(out_dir, entry.key)
        written = not _already_written(path, entry.key)
        if written:
            atomic_write_text(path, _payload_text(entry, ctx))
            warning = emit_emitted(ctx, entry.key, str(path), cause)
            if warning is not None:
                audit_warnings.append(warning)
        files.append(EmittedFile(entry.key, path, written))
        if entry.key not in recorded:
            records.append(
                ReviewDebtRecord(
                    key=entry.key,
                    tracker=ctx.tracker,
                    ticket_id=None,
                    ticket_url=None,
                    filed_at=ctx.now(),
                    mode=EMITTED_MODE,
                )
            )
    return EmitResult(
        refused=False,
        files=files,
        records=records,
        audit_warnings=tuple(audit_warnings),
    )
