"""Crash-safe bookkeeping for the Claude-native review findings workspace.

The coordinator still chooses reviewer roles and adjudicates their findings.
This module owns only the durable workspace transitions: roster persistence,
per-role returns, reconciliation, round promotion, recovery, and terminal
marking. The JSON layout stays beside the existing ``.cw/review-findings``
directory so existing review consumers keep reading the same documents.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from cw._lock_guard import LockRank, lock_guard
from cw.atomic import atomic_write_text
from cw.exceptions import CwError
from cw.review_findings import ReviewerFindingsDocument, ReviewerRunFailure
from cw.review_workspace._constants import (
    _ACTIVE_DIR,
    _ACTIVE_MARKER,
    _ARCHIVE_ROOT,
    _ID_RE,
    _LOCK_PATH,
    _ROLE_STATES,
    _SLUG_RE,
    _STAGING_ROOT,
    _TERMINAL_STATES,
)
from cw.review_workspace._models import (
    TransactionPhase,
    WorkspaceResult,
    _Marker,
    _Transaction,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

__all__ = [
    "TransactionPhase",
    "WorkspaceResult",
]


def _error(message: str) -> CwError:
    return CwError(message)


def _safe_component(value: str, label: str) -> str:
    if not _ID_RE.fullmatch(value) or value in {".", ".."}:
        _error_message = f"invalid review workspace {label}: {value!r}"
        raise _error(_error_message)
    return value


def _role_slug(role: str) -> str:
    normalized = role.strip().lower().replace(" ", "-").replace("/", "-")
    normalized = re.sub(r"-+", "-", normalized)
    if not role.strip() or not _SLUG_RE.fullmatch(normalized):
        _error_message = f"invalid reviewer role name: {role!r}"
        raise _error(_error_message)
    return normalized


def _ensure_cw(root: Path) -> Path:
    cw_dir = root / ".cw"
    if cw_dir.is_symlink():
        _error_message = f"review workspace directory must not be a symlink: {cw_dir}"
        raise _error(_error_message)
    try:
        cw_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _error_message = f"cannot create review workspace directory {cw_dir}: {exc}"
        raise _error(_error_message) from exc
    if not cw_dir.is_dir():
        _error_message = f"review workspace path is not a directory: {cw_dir}"
        raise _error(_error_message)
    return cw_dir


@contextlib.contextmanager
def _workspace_lock(root: Path) -> Iterator[None]:
    _ensure_cw(root)
    lock_path = root / _LOCK_PATH
    with lock_guard("review_workspace", lock_path, LockRank.STATE):
        if _kind(lock_path) not in {"absent", "file"}:
            _error_message = f"review workspace lock is not a regular file: {lock_path}"
            raise _error(_error_message)
        try:
            with lock_path.open("a+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    _error_message = (
                        f"review workspace is locked by another process: {lock_path}"
                    )
                    raise _error(_error_message) from exc
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError as exc:
            _error_message = f"cannot lock review workspace {lock_path}: {exc}"
            raise _error(_error_message) from exc


def _kind(path: Path) -> Literal["absent", "directory", "file", "symlink", "other"]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _read_object(path: Path, label: str) -> dict[str, object]:
    if _kind(path) != "file":
        _error_message = f"{label} is not a regular file: {path}"
        raise _error(_error_message)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _error_message = f"cannot read {label} at {path}: {exc}"
        raise _error(_error_message) from exc
    if not isinstance(value, dict):
        _error_message = f"{label} at {path} must contain a JSON object"
        raise _error(_error_message)
    return value


def _write_object(path: Path, value: dict[str, object], label: str) -> None:
    try:
        atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        _error_message = f"cannot atomically write {label} at {path}: {exc}"
        raise _error(_error_message) from exc


def _raw_role_entries(raw_roster: object, label: str) -> list[tuple[str, str | None]]:
    entries: list[tuple[str, str | None]] = []
    if isinstance(raw_roster, list):
        for item in raw_roster:
            if isinstance(item, str):
                entries.append((item, None))
            elif isinstance(item, dict):
                role = item.get("role", item.get("name"))
                status = item.get("status")
                if not isinstance(role, str):
                    _error_message = f"{label} contains a role without a name"
                    raise _error(_error_message)
                if status is not None and not isinstance(status, str):
                    _error_message = f"{label} contains an invalid state for {role!r}"
                    raise _error(_error_message)
                entries.append((role, status))
            else:
                _error_message = f"{label} contains an invalid role entry: {item!r}"
                raise _error(_error_message)
        return entries
    if isinstance(raw_roster, dict):
        for role, state in raw_roster.items():
            status = state.get("status") if isinstance(state, dict) else state
            if status is not None and not isinstance(status, str):
                _error_message = f"{label} contains an invalid state for {role!r}"
                raise _error(_error_message)
            entries.append((role, status))
        return entries
    _error_message = f"{label} must be a list or an object"
    raise _error(_error_message)


def _string_list(raw: object, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        _error_message = f"{label} must be a list of strings"
        raise _error(_error_message)
    return raw


def _parse_failures(
    raw: dict[str, object], path: Path
) -> tuple[list[ReviewerRunFailure], list[str], list[str]]:
    values = raw.get("failed_reviewers", [])
    if not isinstance(values, list):
        _error_message = f"review round marker at {path} has invalid failed_reviewers"
        raise _error(_error_message)
    failures: list[ReviewerRunFailure] = []
    for value in values:
        try:
            failures.append(ReviewerRunFailure.model_validate(value))
        except ValidationError as exc:
            _error_message = f"invalid reviewer failure in {path}: {exc}"
            raise _error(_error_message) from exc
    failure_roles = [failure.role for failure in failures]
    if len(failure_roles) != len(set(failure_roles)):
        _error_message = f"review round marker at {path} repeats a reviewer failure"
        raise _error(_error_message)
    friction = _string_list(raw.get("friction_highlights", []), "friction_highlights")
    return failures, failure_roles, friction


def _parse_role_states(
    path: Path,
    entries: list[tuple[str, str | None]],
    completed: list[str],
    failure_roles: list[str],
    failures: list[ReviewerRunFailure],
) -> dict[str, str]:
    roles: dict[str, str] = {}
    for role, explicit_status in entries:
        _role_slug(role)
        if role in roles:
            _error_message = f"review round marker at {path} repeats role {role!r}"
            raise _error(_error_message)
        if explicit_status is not None and explicit_status not in _ROLE_STATES:
            _error_message = (
                f"review round marker at {path} has invalid state "
                f"{explicit_status!r} for {role!r}"
            )
            raise _error(_error_message)
        roles[role] = explicit_status or "pending"

    role_names = set(roles)
    outside = (set(completed) | set(failure_roles)) - role_names
    if outside:
        _error_message = (
            f"review round marker at {path} records roles outside roster: "
            f"{sorted(outside)}"
        )
        raise _error(_error_message)
    for role in completed:
        if roles[role] == "failed":
            _error_message = f"review round marker at {path} conflicts on role {role!r}"
            raise _error(_error_message)
        roles[role] = "completed"
    for failure in failures:
        if roles[failure.role] == "completed":
            _error_message = (
                f"review round marker at {path} has completed and failed records "
                f"for {failure.role!r}"
            )
            raise _error(_error_message)
        roles[failure.role] = "failed"

    if len({_role_slug(role) for role in roles}) != len(roles):
        _error_message = f"review round marker at {path} has colliding reviewer slugs"
        raise _error(_error_message)
    return roles


def _parse_marker(path: Path) -> _Marker:
    raw = _read_object(path, "review round marker")
    ticket, round_id, status = (
        raw.get("ticket", raw.get("ticket_id")),
        raw.get("round_id"),
        raw.get("status"),
    )
    if not isinstance(ticket, str) or not isinstance(round_id, str):
        _error_message = f"review round marker at {path} has no valid ticket/round_id"
        raise _error(_error_message)
    _safe_component(ticket, "ticket")
    _safe_component(round_id, "round_id")
    if not isinstance(status, str) or status not in {
        "in_progress",
        *_TERMINAL_STATES,
    }:
        _error_message = f"review round marker at {path} has invalid status {status!r}"
        raise _error(_error_message)
    entries = _raw_role_entries(
        raw.get("SPAWNED_ROLES", raw.get("spawned_roles")), "SPAWNED_ROLES"
    )
    if not entries:
        _error_message = f"review round marker at {path} has an empty reviewer roster"
        raise _error(_error_message)
    completed = _string_list(raw.get("completed_roles"), "completed_roles")
    if len(completed) != len(set(completed)):
        _error_message = f"review round marker at {path} repeats a completed role"
        raise _error(_error_message)
    failures, failure_roles, friction = _parse_failures(raw, path)
    roles = _parse_role_states(path, entries, completed, failure_roles, failures)
    return _Marker(
        raw=raw,
        ticket=ticket,
        round_id=round_id,
        status=status,
        roles=roles,
        failures=tuple(failures),
        friction=tuple(friction),
    )


def _marker_payload(marker: _Marker) -> dict[str, object]:
    payload = dict(marker.raw)
    payload.update(
        {
            "ticket": marker.ticket,
            "round_id": marker.round_id,
            "status": marker.status,
            "SPAWNED_ROLES": [
                {"role": role, "status": marker.roles[role]} for role in marker.roles
            ],
            "completed_roles": [
                role for role, status in marker.roles.items() if status == "completed"
            ],
            "failed_reviewers": [
                failure.model_dump(mode="json") for failure in marker.failures
            ],
            "friction_highlights": list(marker.friction),
        }
    )
    return payload


def _write_marker(path: Path, marker: _Marker) -> None:
    _write_object(path, _marker_payload(marker), "review round marker")


def _change_marker(
    marker: _Marker,
    *,
    roles: dict[str, str] | None = None,
    failures: Sequence[ReviewerRunFailure] | None = None,
    friction: Sequence[str] | None = None,
    status: str | None = None,
) -> _Marker:
    next_roles = marker.roles if roles is None else roles
    next_failures = marker.failures if failures is None else tuple(failures)
    next_friction = marker.friction if friction is None else tuple(friction)
    next_status = marker.status if status is None else status
    return _Marker(
        raw=marker.raw,
        ticket=marker.ticket,
        round_id=marker.round_id,
        status=next_status,
        roles=dict(next_roles),
        failures=tuple(next_failures),
        friction=tuple(next_friction),
    )


def _failure_map(marker: _Marker) -> dict[str, ReviewerRunFailure]:
    return {failure.role: failure for failure in marker.failures}


def _documents(directory: Path, marker: _Marker) -> dict[str, ReviewerFindingsDocument]:
    if _kind(directory) != "directory":
        _error_message = f"findings workspace is not a directory: {directory}"
        raise _error(_error_message)
    slug_roles = {_role_slug(role): role for role in marker.roles}
    documents: dict[str, ReviewerFindingsDocument] = {}
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        _error_message = f"cannot inspect findings workspace {directory}: {exc}"
        raise _error(_error_message) from exc
    for path in entries:
        kind = _kind(path)
        if kind != "file" or path.suffix != ".json":
            _error_message = f"unexpected artifact in findings workspace: {path.name}"
            raise _error(_error_message)
        role = slug_roles.get(path.stem)
        if role is None:
            _error_message = (
                f"findings artifact is outside the reviewer roster: {path.name}"
            )
            raise _error(_error_message)
        raw = _read_object(path, "reviewer findings artifact")
        try:
            document = ReviewerFindingsDocument.model_validate(raw)
        except ValidationError as exc:
            _error_message = f"invalid reviewer findings artifact {path}: {exc}"
            raise _error(_error_message) from exc
        if document.reviewer_role != role:
            _error_message = (
                f"reviewer findings artifact {path} names role "
                f"{document.reviewer_role!r}, expected {role!r}"
            )
            raise _error(_error_message)
        if document.status == "failed":
            _error_message = (
                f"failed reviewer {role!r} must be stored as a failure record, "
                f"not a findings artifact ({path})"
            )
            raise _error(_error_message)
        documents[role] = document
    return documents


def _reconcile_marker(
    marker: _Marker, directory: Path, marker_path: Path
) -> tuple[_Marker, bool]:
    documents = _documents(directory, marker)
    failures = _failure_map(marker)
    conflicts = set(documents) & set(failures)
    if conflicts:
        _error_message = (
            f"reviewer has both findings and failure records: {sorted(conflicts)}"
        )
        raise _error(_error_message)

    roles: dict[str, str] = {}
    for role, prior_status in marker.roles.items():
        if role in documents:
            roles[role] = "completed"
        elif role in failures:
            roles[role] = "failed"
        elif prior_status in {"completed", "failed"}:
            roles[role] = "pending"
        else:
            roles[role] = "pending"
    status = marker.status
    if status in _TERMINAL_STATES and "pending" in roles.values():
        status = "in_progress"
    if status == "completed" and failures:
        status = "failed"
    if status == "completed" and any(
        document.status != "ok" for document in documents.values()
    ):
        _error_message = "completed review round contains a degraded reviewer"
        raise _error(_error_message)
    changed = roles != marker.roles or status != marker.status
    reconciled = _change_marker(marker, roles=roles, status=status)
    if changed:
        _write_marker(marker_path, reconciled)
    return reconciled, changed


def _verify_bundle(
    directory: Path, marker_path: Path, identity: tuple[str, str]
) -> _Marker:
    marker = _parse_marker(marker_path)
    if (marker.ticket, marker.round_id) != identity:
        _error_message = (
            f"review bundle identity mismatch at {marker_path}: "
            f"expected {identity[0]}/{identity[1]}, "
            f"got {marker.ticket}/{marker.round_id}"
        )
        raise _error(_error_message)
    documents = _documents(directory, marker)
    failures = _failure_map(marker)
    conflicts = set(documents) & set(failures)
    if conflicts:
        _error_message = (
            f"review bundle has findings/failure conflicts: {sorted(conflicts)}"
        )
        raise _error(_error_message)
    for role, status in marker.roles.items():
        if status == "completed" and role not in documents:
            _error_message = f"completed reviewer {role!r} has no findings artifact"
            raise _error(_error_message)
        if status == "failed" and role not in failures:
            _error_message = f"failed reviewer {role!r} has no failure record"
            raise _error(_error_message)
        if status == "pending" and (role in documents or role in failures):
            _error_message = f"pending reviewer {role!r} has an unrecorded return"
            raise _error(_error_message)
    if marker.status == "completed":
        if failures or any(status != "completed" for status in marker.roles.values()):
            _error_message = "completed review bundle contains incomplete reviewers"
            raise _error(_error_message)
        if any(document.status != "ok" for document in documents.values()):
            _error_message = "completed review bundle contains a degraded reviewer"
            raise _error(_error_message)
    return marker


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


def _new_marker(ticket: str, round_id: str, roles: Sequence[str]) -> _Marker:
    role_states = dict.fromkeys(roles, "pending")
    transaction = _manifest_path(ticket, round_id).as_posix()
    archive = (_ARCHIVE_ROOT / f"{ticket}-{round_id}").as_posix()
    return _Marker(
        raw={
            "schema_version": 1,
            "ticket": ticket,
            "round_id": round_id,
            "status": "in_progress",
            "SPAWNED_ROLES": [{"role": role, "status": "pending"} for role in roles],
            "completed_roles": [],
            "failed_reviewers": [],
            "friction_highlights": [],
            "archive_path": archive,
            "transaction_manifest": transaction,
        },
        ticket=ticket,
        round_id=round_id,
        status="in_progress",
        roles=role_states,
        failures=(),
        friction=(),
    )


def _workspace_result(
    action: Literal["started", "resumed"], marker: _Marker
) -> WorkspaceResult:
    return WorkspaceResult(
        action=action,
        ticket=marker.ticket,
        round_id=marker.round_id,
        pending_roles=tuple(
            role for role, status in marker.roles.items() if status == "pending"
        ),
        completed_roles=tuple(
            role for role, status in marker.roles.items() if status == "completed"
        ),
        failed_reviewers=marker.failures,
    )


def _active_pair(root: Path) -> tuple[Path, Path]:
    directory = root / _ACTIVE_DIR
    marker = root / _ACTIVE_MARKER
    directory_kind, marker_kind = _kind(directory), _kind(marker)
    if directory_kind == "absent" and marker_kind == "absent":
        _error_message = "no active review workspace exists"
        raise _error(_error_message)
    if directory_kind != "directory" or marker_kind != "file":
        _error_message = (
            "active review workspace pair is inconsistent: "
            f"{directory_kind} / {marker_kind}"
        )
        raise _error(_error_message)
    return directory, marker


def _reconcile_active(root: Path) -> _Marker:
    directory, marker_path = _active_pair(root)
    marker = _parse_marker(marker_path)
    marker, _changed = _reconcile_marker(marker, directory, marker_path)
    return marker


def _validated_roles(roles: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(roles)
    if not selected:
        _error_message = "reviewer roster must contain at least one role"
        raise _error(_error_message)
    for role in selected:
        _role_slug(role)
    if len(selected) != len(set(selected)):
        _error_message = "reviewer roster contains duplicate roles"
        raise _error(_error_message)
    if len({_role_slug(role) for role in selected}) != len(selected):
        _error_message = "reviewer roster contains colliding role slugs"
        raise _error(_error_message)
    return selected


def _resume_or_previous(
    root: Path, ticket: str
) -> tuple[WorkspaceResult | None, tuple[str, str] | None]:
    active_dir = root / _ACTIVE_DIR
    active_marker = root / _ACTIVE_MARKER
    directory_kind, marker_kind = _kind(active_dir), _kind(active_marker)
    if directory_kind == "directory" and marker_kind == "file":
        marker = _reconcile_active(root)
        if marker.status == "in_progress":
            if marker.ticket != ticket:
                _error_message = (
                    f"active review round belongs to ticket {marker.ticket!r}, "
                    f"not {ticket!r}"
                )
                raise _error(_error_message)
            return _workspace_result("resumed", marker), None
        previous = (marker.ticket, marker.round_id)
        return None, previous
    if directory_kind == "absent" and marker_kind == "absent":
        return None, None
    _error_message = (
        f"active review workspace pair is inconsistent: "
        f"{directory_kind} / {marker_kind}"
    )
    raise _error(_error_message)


def _start_round(
    root: Path,
    ticket: str,
    round_id: str,
    roles: tuple[str, ...],
    previous: tuple[str, str] | None,
) -> WorkspaceResult:
    transaction = _new_transaction(ticket, round_id, previous=previous)
    transaction_path = root / _manifest_path(ticket, round_id)
    if _kind(transaction_path.parent) != "absent":
        _error_message = (
            f"review archive identity already exists: {transaction_path.parent}"
        )
        raise _error(_error_message)
    staging_root = root / _STAGING_ROOT / f"{ticket}-{round_id}"
    if _kind(staging_root) != "absent":
        _error_message = f"review staging identity already exists: {staging_root}"
        raise _error(_error_message)
    try:
        (staging_root / "review-findings").mkdir(parents=True)
        _write_marker(
            staging_root / "review-findings-round.json",
            _new_marker(ticket, round_id, roles),
        )
        transaction_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _error_message = f"cannot prepare review workspace transaction: {exc}"
        raise _error(_error_message) from exc
    _write_transaction(root, transaction)
    _recover_transaction(root, transaction)
    return _workspace_result("started", _reconcile_active(root))


def initialize_workspace(
    worktree: Path,
    *,
    ticket: str,
    round_id: str,
    roles: Sequence[str],
) -> WorkspaceResult:
    """Initialize a fresh round or resume the ticket's in-progress round.

    A resumed round keeps its persisted roster and every returned artifact,
    even if the caller's newly selected roster differs.
    """
    ticket = _safe_component(ticket, "ticket")
    round_id = _safe_component(round_id, "round_id")
    selected = _validated_roles(roles)
    root = worktree.resolve()
    with _workspace_lock(root):
        _recover_all(root)
        resumed, previous = _resume_or_previous(root, ticket)
        if resumed is not None:
            return resumed
        return _start_round(root, ticket, round_id, selected, previous)


def reconcile_workspace(worktree: Path) -> WorkspaceResult:
    """Reconcile the active roster with valid findings and failure records."""
    root = worktree.resolve()
    with _workspace_lock(root):
        _recover_all(root)
        marker = _reconcile_active(root)
        return _workspace_result("resumed", marker)


def _require_pending(marker: _Marker, role: str) -> None:
    if role not in marker.roles:
        _error_message = f"reviewer role is outside the persisted roster: {role!r}"
        raise _error(_error_message)
    if marker.status != "in_progress":
        _error_message = f"cannot record a reviewer return on a {marker.status} round"
        raise _error(_error_message)
    if marker.roles[role] != "pending":
        _error_message = f"reviewer role {role!r} is already {marker.roles[role]}"
        raise _error(_error_message)


def record_document(worktree: Path, *, role: str, content: str) -> WorkspaceResult:
    """Validate and atomically record one reviewer document verbatim."""
    role_slug = _role_slug(role)
    root = worktree.resolve()
    with _workspace_lock(root):
        _recover_all(root)
        directory, marker_path = _active_pair(root)
        marker = _parse_marker(marker_path)
        marker, _changed = _reconcile_marker(marker, directory, marker_path)
        _require_pending(marker, role)
        try:
            raw = json.loads(content)
            document = ReviewerFindingsDocument.model_validate(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            _error_message = f"invalid reviewer findings document for {role!r}: {exc}"
            raise _error(_error_message) from exc
        if document.reviewer_role != role or document.status == "failed":
            _error_message = f"reviewer findings document does not match role {role!r}"
            raise _error(_error_message)
        artifact = directory / f"{role_slug}.json"
        if _kind(artifact) != "absent":
            _error_message = f"reviewer findings artifact already exists: {artifact}"
            raise _error(_error_message)
        try:
            atomic_write_text(artifact, content)
        except OSError as exc:
            _error_message = (
                f"cannot atomically write reviewer findings {artifact}: {exc}"
            )
            raise _error(_error_message) from exc
        updated_roles = {**marker.roles, role: "completed"}
        updated = _change_marker(marker, roles=updated_roles)
        _write_marker(marker_path, updated)
        reconciled, _changed = _reconcile_marker(updated, directory, marker_path)
        return _workspace_result("resumed", reconciled)


def record_failure(
    worktree: Path,
    *,
    role: str,
    failure: ReviewerRunFailure,
) -> WorkspaceResult:
    """Record a failed reviewer return in the round marker."""
    _role_slug(role)
    if failure.role != role:
        _error_message = f"reviewer failure names {failure.role!r}, expected {role!r}"
        raise _error(_error_message)
    root = worktree.resolve()
    with _workspace_lock(root):
        _recover_all(root)
        directory, marker_path = _active_pair(root)
        marker = _parse_marker(marker_path)
        marker, _changed = _reconcile_marker(marker, directory, marker_path)
        _require_pending(marker, role)
        artifact = directory / f"{_role_slug(role)}.json"
        if _kind(artifact) != "absent":
            _error_message = f"reviewer role {role!r} already has findings: {artifact}"
            raise _error(_error_message)
        updated_roles = {**marker.roles, role: "failed"}
        friction_note = f"reviewer_failed: {role} ({failure.reason})"
        friction = marker.friction
        if friction_note not in friction:
            friction = (*friction, friction_note)
        updated = _change_marker(
            marker,
            roles=updated_roles,
            failures=(*marker.failures, failure),
            friction=friction,
        )
        _write_marker(marker_path, updated)
        return _workspace_result("resumed", updated)


def finish_workspace(
    worktree: Path, *, status: Literal["completed", "failed"]
) -> WorkspaceResult:
    """Mark the current round terminal after its stage exit."""
    root = worktree.resolve()
    with _workspace_lock(root):
        _recover_all(root)
        directory, marker_path = _active_pair(root)
        marker = _parse_marker(marker_path)
        marker, _changed = _reconcile_marker(marker, directory, marker_path)
        pending = [role for role, state in marker.roles.items() if state == "pending"]
        if pending and status == "completed":
            _error_message = f"cannot finish review with pending reviewers: {pending}"
            raise _error(_error_message)
        if marker.status in _TERMINAL_STATES:
            if marker.status != status:
                _error_message = (
                    f"review round is already terminal with status {marker.status!r}"
                )
                raise _error(_error_message)
            return _workspace_result("resumed", marker)
        if status == "completed" and marker.failures:
            _error_message = (
                "cannot mark review completed while reviewer failures remain"
            )
            raise _error(_error_message)
        if status == "completed":
            docs = _documents(directory, marker)
            degraded = sorted(role for role, doc in docs.items() if doc.status != "ok")
            if degraded:
                _error_message = (
                    f"cannot mark review completed with degraded reviewers: {degraded}"
                )
                raise _error(_error_message)
        finished = _change_marker(marker, status=status)
        _write_marker(marker_path, finished)
        return _workspace_result("resumed", finished)
