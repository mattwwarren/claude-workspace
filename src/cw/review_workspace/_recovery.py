"""The archive and promotion transaction engine and its crash recovery.

``_write_transaction`` and ``_set_phase`` persist each phase; ``_rename``
performs each ordered move. The ``_recover_*`` state machine reads the
on-disk pair state and drives an interrupted transaction forward to
``committed``. ``_recover_all`` scans the archive and staging roots, and
``recover_workspace`` is the public, locked entry point. Imports
``_constants``, ``_models``, ``_primitives``, ``_lock``, ``_manifest`` and
``_marker``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_workspace._constants import (
    _ACTIVE_DIR,
    _ACTIVE_MARKER,
    _ARCHIVE_ROOT,
    _STAGING_ROOT,
)
from cw.review_workspace._lock import _workspace_lock
from cw.review_workspace._manifest import (
    _load_transaction,
    _manifest_path,
    _new_transaction,
    _transaction_paths,
)
from cw.review_workspace._marker import (
    _parse_marker,
    _reconcile_marker,
    _verify_bundle,
)
from cw.review_workspace._primitives import _error, _kind, _write_object

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.review_workspace._models import TransactionPhase, _Transaction


def _write_transaction(root: Path, transaction: _Transaction) -> None:
    path = root / _manifest_path(transaction.ticket, transaction.round_id)
    _write_object(path, transaction.model_dump(mode="json"), "archive transaction")


def _set_phase(
    root: Path, transaction: _Transaction, phase: TransactionPhase
) -> _Transaction:
    updated = transaction.model_copy(update={"phase": phase})
    _write_transaction(root, updated)
    return updated


def _rename(source: Path, destination: Path) -> None:
    if _kind(source) not in {"directory", "file"}:
        _error_message = f"transaction rename source is missing or invalid: {source}"
        raise _error(_error_message)
    if _kind(destination) != "absent":
        _error_message = f"transaction rename destination already exists: {destination}"
        raise _error(_error_message)
    try:
        source.rename(destination)
    except OSError as exc:
        _error_message = f"cannot rename {source} to {destination}: {exc}"
        raise _error(_error_message) from exc


def _pair_state(root: Path, transaction: _Transaction) -> tuple[str, ...]:
    return tuple(
        _kind(root / path)
        for path in (
            transaction.active_directory,
            transaction.active_marker,
            transaction.archive_directory,
            transaction.archive_marker,
            transaction.staging_directory,
            transaction.staging_marker,
        )
    )


def _expected_kind(kind: str, path: Path, expected: str) -> None:
    if kind != expected:
        _error_message = (
            f"transaction path has state {kind!r}; expected {expected!r}: {path}"
        )
        raise _error(_error_message)


def _verify_old(transaction: _Transaction, directory: Path, marker: Path) -> None:
    if transaction.previous_ticket is None or transaction.previous_round_id is None:
        _error_message = "archive transaction is missing its previous round identity"
        raise _error(_error_message)
    _verify_bundle(
        directory,
        marker,
        (transaction.previous_ticket, transaction.previous_round_id),
    )


def _verify_new(transaction: _Transaction, directory: Path, marker: Path) -> None:
    _verify_bundle(directory, marker, (transaction.ticket, transaction.round_id))


def _verify_transaction_paths(root: Path, transaction: _Transaction) -> None:
    for relative in (
        transaction.active_directory,
        transaction.active_marker,
        transaction.archive_directory,
        transaction.archive_marker,
        transaction.staging_directory,
        transaction.staging_marker,
    ):
        path = root / relative
        if _kind(path) in {"symlink", "other"}:
            _error_message = f"invalid filesystem object in archive transaction: {path}"
            raise _error(_error_message)


def _recover_committed(root: Path, transaction: _Transaction) -> None:
    paths = {
        key: root / value
        for key, value in _transaction_paths(
            transaction.ticket, transaction.round_id
        ).items()
    }
    _expected_kind(
        _kind(paths["staging_directory"]), paths["staging_directory"], "absent"
    )
    _expected_kind(_kind(paths["staging_marker"]), paths["staging_marker"], "absent")
    active_marker = paths["active_marker"]
    if _kind(active_marker) == "file":
        active = _parse_marker(active_marker)
        if (active.ticket, active.round_id) == (
            transaction.ticket,
            transaction.round_id,
        ):
            _reconcile_marker(active, paths["active_directory"], active_marker)
        else:
            _verify_archived_new(root, transaction)
    else:
        _verify_archived_new(root, transaction)
    if transaction.has_old_round:
        _verify_old(
            transaction,
            paths["archive_directory"],
            paths["archive_marker"],
        )
    else:
        _expected_kind(
            _kind(paths["archive_directory"]), paths["archive_directory"], "absent"
        )
        _expected_kind(
            _kind(paths["archive_marker"]), paths["archive_marker"], "absent"
        )


def _verify_archived_new(root: Path, transaction: _Transaction) -> None:
    archive_root = root / _ARCHIVE_ROOT
    identity = (transaction.ticket, transaction.round_id)
    matches: list[tuple[Path, Path]] = []
    for marker_path in archive_root.glob("*/review-findings-round.json"):
        if _kind(marker_path) != "file":
            continue
        marker = _parse_marker(marker_path)
        if (marker.ticket, marker.round_id) == identity:
            matches.append((marker_path.parent / "review-findings", marker_path))
    if len(matches) != 1:
        _error_message = (
            f"committed review bundle {identity[0]}/{identity[1]} has "
            f"{len(matches)} archived copies"
        )
        raise _error(_error_message)
    _verify_bundle(matches[0][0], matches[0][1], identity)


def _recover_no_old(root: Path, transaction: _Transaction) -> _Transaction:
    paths = {
        key: root / value
        for key, value in _transaction_paths(
            transaction.ticket, transaction.round_id
        ).items()
    }
    state = _pair_state(root, transaction)
    empty = ("absent", "absent", "absent", "absent", "directory", "file")
    new_directory = ("directory", "absent", "absent", "absent", "absent", "file")
    new_pair = ("directory", "file", "absent", "absent", "absent", "absent")
    if state == empty:
        if transaction.phase == "prepared":
            transaction = _set_phase(root, transaction, "archive_committed")
        elif transaction.phase != "archive_committed":
            _error_message = (
                f"phase {transaction.phase!r} is inconsistent with first-run state"
            )
            raise _error(_error_message)
        _verify_new(transaction, paths["staging_directory"], paths["staging_marker"])
        _rename(paths["staging_directory"], paths["active_directory"])
        return _set_phase(root, transaction, "staging_directory_promoted")
    if state == new_directory:
        if transaction.phase == "archive_committed":
            transaction = _set_phase(root, transaction, "staging_directory_promoted")
        elif transaction.phase != "staging_directory_promoted":
            _error_message = (
                f"phase {transaction.phase!r} is inconsistent with promotion state"
            )
            raise _error(_error_message)
        _verify_new(transaction, paths["active_directory"], paths["staging_marker"])
        _rename(paths["staging_marker"], paths["active_marker"])
        return _set_phase(root, transaction, "staging_marker_promoted")
    if state == new_pair:
        _verify_new(transaction, paths["active_directory"], paths["active_marker"])
        if transaction.phase == "staging_directory_promoted":
            transaction = _set_phase(root, transaction, "staging_marker_promoted")
        elif transaction.phase not in {"staging_marker_promoted", "committed"}:
            _error_message = (
                f"phase {transaction.phase!r} is inconsistent with committed files"
            )
            raise _error(_error_message)
        if transaction.phase != "committed":
            transaction = _set_phase(root, transaction, "committed")
        return transaction
    _error_message = f"inconsistent first-run transaction state: {state!r}"
    raise _error(_error_message)


def _old_transaction_paths(root: Path, transaction: _Transaction) -> dict[str, Path]:
    return {
        key: root / value
        for key, value in _transaction_paths(
            transaction.ticket, transaction.round_id
        ).items()
    }


def _recover_old_active_pair(
    root: Path, transaction: _Transaction, paths: dict[str, Path]
) -> _Transaction:
    if transaction.phase != "prepared":
        _error_message = (
            f"phase {transaction.phase!r} is inconsistent with old active pair"
        )
        raise _error(_error_message)
    _verify_old(transaction, paths["active_directory"], paths["active_marker"])
    _verify_new(transaction, paths["staging_directory"], paths["staging_marker"])
    _rename(paths["active_directory"], paths["archive_directory"])
    return _set_phase(root, transaction, "active_directory_archived")


def _recover_old_directory_moved(
    root: Path, transaction: _Transaction, paths: dict[str, Path]
) -> _Transaction:
    if transaction.phase not in {"prepared", "active_directory_archived"}:
        _error_message = (
            f"phase {transaction.phase!r} is inconsistent after directory archive"
        )
        raise _error(_error_message)
    _verify_old(transaction, paths["archive_directory"], paths["active_marker"])
    _verify_new(transaction, paths["staging_directory"], paths["staging_marker"])
    if transaction.phase == "prepared":
        transaction = _set_phase(root, transaction, "active_directory_archived")
    _rename(paths["active_marker"], paths["archive_marker"])
    return _set_phase(root, transaction, "active_marker_archived")


def _recover_old_archived(
    root: Path, transaction: _Transaction, paths: dict[str, Path]
) -> _Transaction:
    allowed = {
        "active_directory_archived",
        "active_marker_archived",
        "archive_committed",
    }
    if transaction.phase not in allowed:
        _error_message = f"phase {transaction.phase!r} is inconsistent after archive"
        raise _error(_error_message)
    _verify_old(transaction, paths["archive_directory"], paths["archive_marker"])
    _verify_new(transaction, paths["staging_directory"], paths["staging_marker"])
    if transaction.phase == "active_directory_archived":
        transaction = _set_phase(root, transaction, "active_marker_archived")
    if transaction.phase == "active_marker_archived":
        transaction = _set_phase(root, transaction, "archive_committed")
    _rename(paths["staging_directory"], paths["active_directory"])
    return _set_phase(root, transaction, "staging_directory_promoted")


def _recover_old_new_directory(
    root: Path, transaction: _Transaction, paths: dict[str, Path]
) -> _Transaction:
    allowed = {"archive_committed", "staging_directory_promoted"}
    if transaction.phase not in allowed:
        _error_message = (
            f"phase {transaction.phase!r} is inconsistent after staging directory move"
        )
        raise _error(_error_message)
    _verify_old(transaction, paths["archive_directory"], paths["archive_marker"])
    _verify_new(transaction, paths["active_directory"], paths["staging_marker"])
    if transaction.phase == "archive_committed":
        transaction = _set_phase(root, transaction, "staging_directory_promoted")
    _rename(paths["staging_marker"], paths["active_marker"])
    return _set_phase(root, transaction, "staging_marker_promoted")


def _recover_old_new_pair(
    root: Path, transaction: _Transaction, paths: dict[str, Path]
) -> _Transaction:
    allowed = {"staging_directory_promoted", "staging_marker_promoted", "committed"}
    if transaction.phase not in allowed:
        _error_message = (
            f"phase {transaction.phase!r} is inconsistent after marker promotion"
        )
        raise _error(_error_message)
    _verify_old(transaction, paths["archive_directory"], paths["archive_marker"])
    _verify_new(transaction, paths["active_directory"], paths["active_marker"])
    if transaction.phase == "staging_directory_promoted":
        transaction = _set_phase(root, transaction, "staging_marker_promoted")
    if transaction.phase == "staging_marker_promoted":
        transaction = _set_phase(root, transaction, "committed")
    return transaction


def _recover_old(root: Path, transaction: _Transaction) -> _Transaction:
    paths = _old_transaction_paths(root, transaction)
    states: dict[
        tuple[str, ...], Callable[[Path, _Transaction, dict[str, Path]], _Transaction]
    ] = {
        (
            "directory",
            "file",
            "absent",
            "absent",
            "directory",
            "file",
        ): _recover_old_active_pair,
        (
            "absent",
            "file",
            "directory",
            "absent",
            "directory",
            "file",
        ): _recover_old_directory_moved,
        (
            "absent",
            "absent",
            "directory",
            "file",
            "directory",
            "file",
        ): _recover_old_archived,
        (
            "directory",
            "absent",
            "directory",
            "file",
            "absent",
            "file",
        ): _recover_old_new_directory,
        (
            "directory",
            "file",
            "directory",
            "file",
            "absent",
            "absent",
        ): _recover_old_new_pair,
    }
    handler = states.get(_pair_state(root, transaction))
    if handler is None:
        _error_message = (
            "inconsistent archived-round transaction state: "
            f"{_pair_state(root, transaction)!r}"
        )
        raise _error(_error_message)
    return handler(root, transaction, paths)


def _recover_transaction(root: Path, transaction: _Transaction) -> None:
    _verify_transaction_paths(root, transaction)
    if transaction.phase == "committed":
        _recover_committed(root, transaction)
        return
    for _ in range(12):
        transaction = (
            _recover_old(root, transaction)
            if transaction.has_old_round
            else _recover_no_old(root, transaction)
        )
        if transaction.phase == "committed":
            _recover_committed(root, transaction)
            return
    _error_message = "review workspace recovery exceeded its deterministic phase limit"
    raise _error(_error_message)


def _transaction_from_staging(root: Path, staging: Path) -> _Transaction:
    marker_path = staging / "review-findings-round.json"
    marker = _parse_marker(marker_path)
    if staging.name != f"{marker.ticket}-{marker.round_id}":
        _error_message = f"staging path does not match marker identity: {staging}"
        raise _error(_error_message)
    findings = staging / "review-findings"
    _verify_bundle(findings, marker_path, (marker.ticket, marker.round_id))
    if any(path.suffix == ".json" for path in findings.iterdir()):
        _error_message = (
            f"unpromoted staging round already has reviewer artifacts: {findings}"
        )
        raise _error(_error_message)

    active_kind = _kind(root / _ACTIVE_DIR)
    marker_kind = _kind(root / _ACTIVE_MARKER)
    previous: tuple[str, str] | None = None
    if active_kind == "directory" and marker_kind == "file":
        active = _parse_marker(root / _ACTIVE_MARKER)
        if active.status == "in_progress":
            _error_message = (
                "cannot recover a staging round over an in-progress active round"
            )
            raise _error(_error_message)
        previous = (active.ticket, active.round_id)
        _verify_old(
            _new_transaction(marker.ticket, marker.round_id, previous=previous),
            root / _ACTIVE_DIR,
            root / _ACTIVE_MARKER,
        )
    elif active_kind != "absent" or marker_kind != "absent":
        _error_message = "cannot recover staging round from an inconsistent active pair"
        raise _error(_error_message)

    transaction = _new_transaction(marker.ticket, marker.round_id, previous=previous)
    transaction_dir = (root / _manifest_path(marker.ticket, marker.round_id)).parent
    transaction_dir.mkdir(parents=True, exist_ok=True)
    expected = _transaction_paths(marker.ticket, marker.round_id)
    for key, relative in expected.items():
        if key in {"staging_directory", "staging_marker"} or (
            previous is not None and key in {"active_directory", "active_marker"}
        ):
            continue
        if _kind(root / relative) != "absent":
            _error_message = f"orphan staging transaction conflicts with {relative}"
            raise _error(_error_message)
    _write_transaction(root, transaction)
    return transaction


def _transaction_roots(root: Path) -> tuple[Path, Path]:
    archive_root = root / _ARCHIVE_ROOT
    stage_root = root / _STAGING_ROOT
    for directory in (archive_root, stage_root):
        kind = _kind(directory)
        if kind in {"symlink", "other", "file"}:
            _error_message = (
                f"review transaction root is not a real directory: {directory}"
            )
            raise _error(_error_message)
        directory.mkdir(parents=True, exist_ok=True)
    return archive_root, stage_root


def _scan_archive_transactions(
    root: Path, archive_root: Path
) -> tuple[list[_Transaction], list[_Transaction], set[str]]:
    pending: list[_Transaction] = []
    committed: list[_Transaction] = []
    unfinished_manifests: set[str] = set()
    for entry in sorted(archive_root.iterdir(), key=lambda item: item.name):
        if _kind(entry) != "directory":
            _error_message = f"invalid entry in review archive root: {entry}"
            raise _error(_error_message)
        manifest = entry / "archive-transaction.json"
        if _kind(manifest) == "file":
            transaction = _load_transaction(root, manifest)
            target = committed if transaction.phase == "committed" else pending
            target.append(transaction)
        elif any(
            child.name.startswith("archive-transaction.json.")
            for child in entry.iterdir()
        ) or not list(entry.iterdir()):
            unfinished_manifests.add(entry.name)
        else:
            _error_message = (
                f"archive transaction directory has no valid manifest: {entry}"
            )
            raise _error(_error_message)
    return pending, committed, unfinished_manifests


def _inspect_staging_entry(
    root: Path,
    archive_root: Path,
    staging: Path,
    unfinished_manifests: set[str],
) -> Path | None:
    if _kind(staging) != "directory":
        _error_message = f"invalid entry in review staging root: {staging}"
        raise _error(_error_message)
    marker_path = staging / "review-findings-round.json"
    if _kind(marker_path) != "file":
        tx_path = archive_root / staging.name / "archive-transaction.json"
        if not list(staging.iterdir()) and _kind(tx_path) == "file":
            transaction = _load_transaction(root, tx_path)
            if (
                transaction.phase == "committed"
                and staging.name == f"{transaction.ticket}-{transaction.round_id}"
            ):
                return None
        _error_message = f"staging bundle has no valid marker: {staging}"
        raise _error(_error_message)

    marker = _parse_marker(marker_path)
    tx_name = f"{marker.ticket}-{marker.round_id}"
    tx_path = root / _manifest_path(marker.ticket, marker.round_id)
    if _kind(tx_path) == "file":
        transaction = _load_transaction(root, tx_path)
        if transaction.phase != "committed":
            _recover_transaction(root, transaction)
            return None
        _error_message = f"committed transaction still has staging data: {staging}"
        raise _error(_error_message)
    if tx_name in unfinished_manifests or _kind(tx_path.parent) == "absent":
        return staging
    if _kind(tx_path.parent) == "directory" and not list(tx_path.parent.iterdir()):
        unfinished_manifests.add(tx_name)
        return staging
    _error_message = (
        f"staging bundle has an unrecognized transaction directory: {staging}"
    )
    raise _error(_error_message)


def _recover_staging_entries(
    root: Path,
    archive_root: Path,
    stage_root: Path,
    unfinished_manifests: set[str],
) -> None:
    orphans = [
        orphan
        for staging in sorted(stage_root.iterdir(), key=lambda item: item.name)
        if (
            orphan := _inspect_staging_entry(
                root, archive_root, staging, unfinished_manifests
            )
        )
        is not None
    ]
    if len(orphans) > 1:
        _error_message = "multiple orphan review staging bundles need recovery"
        raise _error(_error_message)
    if orphans:
        transaction = _transaction_from_staging(root, orphans[0])
        _recover_transaction(root, transaction)
        unfinished_manifests.discard(orphans[0].name)
    if unfinished_manifests:
        _error_message = (
            "archive transaction manifest is missing for: "
            f"{sorted(unfinished_manifests)}"
        )
        raise _error(_error_message)


def _recover_all(root: Path) -> None:
    archive_root, stage_root = _transaction_roots(root)
    pending, committed, unfinished = _scan_archive_transactions(root, archive_root)
    if len(pending) > 1:
        _error_message = "multiple incomplete review archive transactions need recovery"
        raise _error(_error_message)
    if pending:
        _recover_transaction(root, pending[0])
    for transaction in committed:
        _recover_committed(root, transaction)
    _recover_staging_entries(root, archive_root, stage_root, unfinished)


def recover_workspace(worktree: Path) -> None:
    """Recover any interrupted archive/promotion transaction; idempotent."""
    root = worktree.resolve()
    with _workspace_lock(root):
        _recover_all(root)
