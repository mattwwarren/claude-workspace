"""The round API: initialize, reconcile, record returns and finish.

``initialize_workspace`` starts a fresh round (``_start_round`` stages it and
drives its promotion transaction) or resumes the ticket's in-progress one;
``reconcile_workspace``, ``record_document``, ``record_failure``,
``record_reprompt`` and ``finish_workspace`` act on the active round. Every
entry point takes the workspace lock and recovers any interrupted transaction
first. Imports every other submodule.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from cw.atomic import atomic_write_text
from cw.review_findings import ReviewerFindingsDocument
from cw.review_workspace._constants import (
    _ACTIVE_DIR,
    _ACTIVE_MARKER,
    _ARCHIVE_ROOT,
    _STAGING_ROOT,
    _TERMINAL_STATES,
)
from cw.review_workspace._lock import _workspace_lock
from cw.review_workspace._manifest import _manifest_path, _new_transaction
from cw.review_workspace._marker import (
    _change_marker,
    _documents,
    _parse_marker,
    _reconcile_marker,
    _reprompted_roles,
    _write_marker,
)
from cw.review_workspace._models import WorkspaceResult, _Marker
from cw.review_workspace._primitives import (
    _error,
    _kind,
    _role_slug,
    _safe_component,
)
from cw.review_workspace._recovery import (
    _recover_all,
    _recover_transaction,
    _write_transaction,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.review_findings import ReviewerRunFailure


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


def record_reprompt(worktree: Path, *, role: str) -> tuple[WorkspaceResult, bool]:
    """Grant the one frame-missing re-prompt a pending role gets this round (#3096).

    Returns ``(result, True)`` on the first grant. A repeat returns
    ``(result, False)`` with no write, so the marker bytes stay unchanged.
    """
    _role_slug(role)
    root = worktree.resolve()
    with _workspace_lock(root):
        _recover_all(root)
        directory, marker_path = _active_pair(root)
        marker = _parse_marker(marker_path)
        marker, _changed = _reconcile_marker(marker, directory, marker_path)
        _require_pending(marker, role)
        reprompted = _reprompted_roles(marker)
        if role in reprompted:
            return _workspace_result("resumed", marker), False
        friction_note = f"reviewer_reprompted: {role} (frame_missing)"
        friction = marker.friction
        if friction_note not in friction:
            friction = (*friction, friction_note)
        granted = replace(
            marker,
            raw={**marker.raw, "reprompted_roles": [*reprompted, role]},
        )
        updated = _change_marker(granted, friction=friction)
        _write_marker(marker_path, updated)
        return _workspace_result("resumed", updated), True


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
