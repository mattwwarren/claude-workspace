"""Archive transaction manifest paths, construction and validated loading.

Imports ``_constants``, ``_models`` and ``_primitives``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from cw.review_workspace._constants import (
    _ACTIVE_DIR,
    _ACTIVE_MARKER,
    _ARCHIVE_ROOT,
    _STAGING_ROOT,
)
from cw.review_workspace._models import _Transaction
from cw.review_workspace._primitives import _error, _read_object, _safe_component


def _transaction_paths(ticket: str, round_id: str) -> dict[str, Path]:
    name = f"{ticket}-{round_id}"
    archive_root = _ARCHIVE_ROOT / name
    staging_root = _STAGING_ROOT / name
    return {
        "active_directory": _ACTIVE_DIR,
        "active_marker": _ACTIVE_MARKER,
        "archive_directory": archive_root / "review-findings",
        "archive_marker": archive_root / "review-findings-round.json",
        "staging_directory": staging_root / "review-findings",
        "staging_marker": staging_root / "review-findings-round.json",
    }


def _manifest_path(ticket: str, round_id: str) -> Path:
    return _ARCHIVE_ROOT / f"{ticket}-{round_id}" / "archive-transaction.json"


def _new_transaction(
    ticket: str,
    round_id: str,
    *,
    previous: tuple[str, str] | None,
) -> _Transaction:
    paths = _transaction_paths(ticket, round_id)
    return _Transaction(
        ticket=ticket,
        round_id=round_id,
        phase="prepared",
        has_old_round=previous is not None,
        previous_ticket=previous[0] if previous else None,
        previous_round_id=previous[1] if previous else None,
        **{key: value.as_posix() for key, value in paths.items()},
    )


def _load_transaction(root: Path, path: Path) -> _Transaction:
    raw = _read_object(path, "archive transaction manifest")
    try:
        transaction = _Transaction.model_validate(raw)
    except ValidationError as exc:
        _error_message = f"invalid archive transaction manifest {path}: {exc}"
        raise _error(_error_message) from exc
    _safe_component(transaction.ticket, "transaction ticket")
    _safe_component(transaction.round_id, "transaction round_id")
    if transaction.has_old_round != (
        transaction.previous_ticket is not None
        and transaction.previous_round_id is not None
    ):
        _error_message = (
            f"archive transaction has inconsistent previous identity: {path}"
        )
        raise _error(_error_message)
    if transaction.previous_ticket is not None:
        _safe_component(transaction.previous_ticket, "previous ticket")
        _safe_component(transaction.previous_round_id or "", "previous round_id")
    expected = _transaction_paths(transaction.ticket, transaction.round_id)
    for key, value in expected.items():
        stored = getattr(transaction, key)
        if (
            stored != value.as_posix()
            or Path(stored).is_absolute()
            or ".." in Path(stored).parts
        ):
            _error_message = f"archive transaction has invalid {key} path: {stored!r}"
            raise _error(_error_message)
    if path != root / _manifest_path(transaction.ticket, transaction.round_id):
        _error_message = f"archive transaction identity does not match its path: {path}"
        raise _error(_error_message)
    return transaction
