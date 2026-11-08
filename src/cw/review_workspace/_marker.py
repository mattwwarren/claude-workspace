"""The round marker: parse, payload, write, change, reconcile and verify.

``_parse_marker`` validates a marker file into a ``_Marker``;
``_write_marker`` writes one back. ``_reconcile_marker`` brings a marker's
role states in line with the findings directory, and ``_verify_bundle``
checks a directory and marker pair for consistency. Imports ``_constants``,
``_models`` and ``_primitives``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.review_findings import ReviewerFindingsDocument, ReviewerRunFailure
from cw.review_workspace._constants import _ROLE_STATES, _TERMINAL_STATES
from cw.review_workspace._models import _Marker
from cw.review_workspace._primitives import (
    _error,
    _kind,
    _read_object,
    _role_slug,
    _safe_component,
    _write_object,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


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
