"""Shared best-effort persistence for codex-review session diagnostics."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import PurePath

from cw.config import diagnostics_dir

_log = logging.getLogger(__name__)


def _discriminated_filename(filename: str, discriminator: str | None) -> str:
    """Insert *discriminator* before *filename*'s suffix when supplied."""
    if discriminator is None:
        return filename
    path = PurePath(filename)
    return f"{path.stem}-{discriminator}{path.suffix}"


def _persist_session_diagnostics_json(
    *,
    session_id: str,
    filename: str,
    payload: dict[str, object],
    log_label: str,
    discriminator: str | None = None,
) -> None:
    """Write one session diagnostics JSON artifact; never raise."""
    record = {
        "session_id": session_id,
        **payload,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    try:
        target = diagnostics_dir(session_id)
        target.mkdir(parents=True, exist_ok=True)
        target.joinpath(
            _discriminated_filename(filename=filename, discriminator=discriminator)
        ).write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError:
        _log.warning(
            "%s diagnostics write failed for session %s", log_label, session_id
        )
