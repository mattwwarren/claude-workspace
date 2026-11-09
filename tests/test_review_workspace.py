"""Behavioral coverage for deterministic review-workspace bookkeeping."""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

import pytest
from click.testing import CliRunner

from cw import review_workspace as workspace_module
from cw.cli import main
from cw.exceptions import CwError
from cw.review_findings import ReviewerFindingsDocument, ReviewerRunFailure
from cw.review_workspace import (
    _lock,
    _primitives,
    _recovery,
    _rounds,
    finish_workspace,
    initialize_workspace,
    reconcile_workspace,
    record_document,
    record_failure,
    record_reprompt,
    recover_workspace,
)

T = TypeVar("T")


def _document(role: str, *, status: Literal["ok", "degraded"] = "ok") -> str:
    detail = (
        "checked the assigned review scope"
        if status == "ok"
        else "required check unavailable"
    )
    return ReviewerFindingsDocument(
        reviewer_role=role, status=status, detail=detail
    ).model_dump_json()


def _marker(root: Path) -> dict[str, object]:
    return json.loads((root / ".cw/review-findings-round.json").read_text())


def _write_marker(root: Path, payload: dict[str, object]) -> Path:
    path = root / ".cw/review-findings-round.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _seed_completed(root: Path) -> None:
    initialize_workspace(
        root, ticket="T-1", round_id="round-1", roles=["Code Reviewer", "Security"]
    )
    record_document(root, role="Code Reviewer", content=_document("Code Reviewer"))
    record_failure(
        root,
        role="Security",
        failure=ReviewerRunFailure(role="Security", reason="timeout"),
    )
    finish_workspace(root, status="failed")


def _write_staging_round(root: Path, *, ticket: str, round_id: str) -> Path:
    staging = root / f".cw/review-findings-staging/{ticket}-{round_id}"
    (staging / "review-findings").mkdir(parents=True)
    workspace_module._write_marker(
        staging / "review-findings-round.json",
        workspace_module._new_marker(ticket, round_id, ("Code Reviewer",)),
    )
    return staging


def test_init_persists_roster_and_resume_preserves_returns(tmp_path: Path) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer", "Security"]
    )
    saved = record_document(
        tmp_path, role="Code Reviewer", content=_document("Code Reviewer")
    )

    resumed = initialize_workspace(
        tmp_path,
        ticket="T-1",
        round_id="round-2",
        roles=["Different Roster"],
    )

    assert resumed.action == "resumed"
    assert resumed.pending_roles == ("Security",)
    assert resumed.completed_roles == ("Code Reviewer",)
    assert _marker(tmp_path)["round_id"] == "round-1"
    assert (tmp_path / ".cw/review-findings/code-reviewer.json").is_file()
    assert saved.completed_roles == ("Code Reviewer",)


@pytest.mark.parametrize(
    "roster_field",
    [
        {"spawned_roles": ["Reviewer"]},
        {"SPAWNED_ROLES": {"Reviewer": "pending"}},
    ],
    ids=["legacy-list", "legacy-map"],
)
def test_legacy_marker_shapes_resume_and_preserve_unknown_metadata(
    tmp_path: Path, roster_field: dict[str, object]
) -> None:
    (tmp_path / ".cw/review-findings").mkdir(parents=True)
    _write_marker(
        tmp_path,
        {
            "ticket_id": "T-1",
            "round_id": "round-1",
            "status": "in_progress",
            "completed_roles": [],
            "failed_reviewers": [],
            "friction_highlights": [],
            "legacy_metadata": "keep me",
            **roster_field,
        },
    )

    resumed = reconcile_workspace(tmp_path)
    recorded = record_document(tmp_path, role="Reviewer", content=_document("Reviewer"))

    assert resumed.pending_roles == ("Reviewer",)
    assert recorded.completed_roles == ("Reviewer",)
    assert _marker(tmp_path)["legacy_metadata"] == "keep me"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "done",
                "SPAWNED_ROLES": ["R"],
            },
            "invalid status",
            id="invalid-round-status",
        ),
        pytest.param(
            {"ticket": "T-1", "round_id": "r", "status": [], "SPAWNED_ROLES": ["R"]},
            "invalid status",
            id="unhashable-round-status",
        ),
        pytest.param(
            {"ticket": "T-1", "round_id": "r", "status": "in_progress"},
            "must be a list or an object",
            id="missing-roster",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": [],
            },
            "empty reviewer roster",
            id="empty-roster",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": [{"status": "pending"}],
            },
            "without a name",
            id="role-without-name",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": [{"role": "R", "status": 1}],
            },
            "invalid state",
            id="non-string-role-state",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": [None],
            },
            "invalid role entry",
            id="invalid-role-entry",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R", "R"],
            },
            "repeats role",
            id="duplicate-role",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": [{"role": "R", "status": "invalid"}],
            },
            "invalid state",
            id="invalid-role-state",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "completed_roles": ["Other"],
            },
            "outside roster",
            id="completed-outside-roster",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "completed_roles": ["R", "R"],
            },
            "repeats a completed role",
            id="duplicate-completed-role",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "failed_reviewers": "bad",
            },
            "invalid failed_reviewers",
            id="invalid-failure-list",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "failed_reviewers": [{"role": "R"}],
            },
            "invalid reviewer failure",
            id="invalid-failure-record",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "completed_roles": ["R"],
                "failed_reviewers": [{"role": "R", "reason": "timeout"}],
            },
            "completed and failed records",
            id="completed-failed-conflict",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": [{"role": "R", "status": "failed"}],
                "completed_roles": ["R"],
            },
            "conflicts on role",
            id="completed-role-marked-failed",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["SysAdmin / Scope", "SysAdmin-Scope"],
            },
            "colliding reviewer slugs",
            id="colliding-role-slugs",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "failed_reviewers": [{"role": "Other", "reason": "timeout"}],
            },
            "outside roster",
            id="failure-outside-roster",
        ),
        pytest.param(
            {
                "ticket": "../escape",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
            },
            "invalid review workspace ticket",
            id="unsafe-marker-ticket",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "failed_reviewers": [
                    {"role": "R", "reason": "timeout"},
                    {"role": "R", "reason": "exception"},
                ],
            },
            "repeats a reviewer failure",
            id="duplicate-failures",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "friction_highlights": [1],
            },
            "friction_highlights must be a list of strings",
            id="invalid-friction-list",
        ),
        pytest.param(
            {
                "ticket": "T-1",
                "round_id": "r",
                "status": "in_progress",
                "SPAWNED_ROLES": ["R"],
                "completed_roles": "R",
            },
            "completed_roles must be a list of strings",
            id="invalid-completed-list",
        ),
    ],
)
def test_malformed_markers_fail_closed(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    marker = _write_marker(tmp_path, payload)

    with pytest.raises(CwError, match=message):
        workspace_module._parse_marker(marker)


def test_marker_must_be_a_regular_utf8_json_file(tmp_path: Path) -> None:
    marker = tmp_path / "round.json"
    marker.mkdir()
    with pytest.raises(CwError, match="not a regular file"):
        workspace_module._parse_marker(marker)
    marker.rmdir()
    marker.write_bytes(b"\xff")
    with pytest.raises(CwError, match="cannot read review round marker"):
        workspace_module._parse_marker(marker)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("identity", "review bundle identity mismatch"),
        ("completed_missing", "has no findings artifact"),
        ("failed_missing", "has no failure record"),
        ("pending_has_document", "pending reviewer.*unrecorded return"),
        ("completed_incomplete", "contains incomplete reviewers"),
        ("completed_degraded", "contains a degraded reviewer"),
        ("findings_failure_conflict", "findings/failure conflicts"),
    ],
)
def test_bundle_verification_fails_closed_on_inconsistent_returns(
    tmp_path: Path, case: str, message: str
) -> None:
    directory = tmp_path / "review-findings"
    directory.mkdir()
    marker_path = tmp_path / "review-findings-round.json"
    marker = workspace_module._new_marker("T-1", "round-1", ("Reviewer",))
    identity = ("T-1", "round-1")
    if case == "identity":
        identity = ("T-2", "round-1")
    elif case == "completed_missing":
        marker = workspace_module._change_marker(
            marker, roles={"Reviewer": "completed"}
        )
    elif case == "failed_missing":
        marker = workspace_module._change_marker(marker, roles={"Reviewer": "failed"})
    elif case == "pending_has_document":
        (directory / "reviewer.json").write_text(_document("Reviewer"))
    elif case == "completed_incomplete":
        marker = workspace_module._change_marker(marker, status="completed")
    elif case == "completed_degraded":
        marker = workspace_module._change_marker(
            marker, roles={"Reviewer": "completed"}, status="completed"
        )
        (directory / "reviewer.json").write_text(
            _document("Reviewer", status="degraded")
        )
    elif case == "findings_failure_conflict":
        failure = ReviewerRunFailure(role="Reviewer", reason="timeout")
        marker = workspace_module._change_marker(
            marker, roles={"Reviewer": "failed"}, failures=(failure,)
        )
        (directory / "reviewer.json").write_text(_document("Reviewer"))
    workspace_module._write_marker(marker_path, marker)

    with pytest.raises(CwError, match=message):
        workspace_module._verify_bundle(directory, marker_path, identity)


def test_cli_persists_stdin_document_and_emits_workspace_state(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    initialized = runner.invoke(
        main,
        [
            "review",
            "workspace",
            "init",
            "--ticket",
            "T-1",
            "--round-id",
            "round-1",
            "--role",
            "Code Reviewer",
            "--worktree",
            str(tmp_path),
        ],
    )
    assert initialized.exit_code == 0, initialized.stderr
    assert json.loads(initialized.stdout)["pending_roles"] == ["Code Reviewer"]

    recorded = runner.invoke(
        main,
        [
            "review",
            "workspace",
            "record-document",
            "Code Reviewer",
            "-",
            "--worktree",
            str(tmp_path),
        ],
        input=_document("Code Reviewer"),
    )
    assert recorded.exit_code == 0, recorded.stderr
    assert json.loads(recorded.stdout)["completed_roles"] == ["Code Reviewer"]
    stored = tmp_path / ".cw/review-findings/code-reviewer.json"
    assert stored.read_text() == _document("Code Reviewer")
    finished = runner.invoke(
        main,
        [
            "review",
            "workspace",
            "finish",
            "--status",
            "completed",
            "--worktree",
            str(tmp_path),
        ],
    )
    assert finished.exit_code == 0, finished.stderr
    assert _marker(tmp_path)["status"] == "completed"


def test_sysadmin_scope_role_has_a_stable_slug(tmp_path: Path) -> None:
    initialize_workspace(
        tmp_path,
        ticket="T-1",
        round_id="round-1",
        roles=["SysAdmin / Scope"],
    )
    record_document(
        tmp_path,
        role="SysAdmin / Scope",
        content=_document("SysAdmin / Scope"),
    )

    assert (
        tmp_path / ".cw/review-findings/sysadmin-scope.json"
    ).read_text() == _document("SysAdmin / Scope")


@pytest.mark.parametrize(
    ("ticket", "round_id"),
    [("../T-1", "round-1"), ("T-1", "../round-1"), ("", "round-1")],
)
def test_invalid_workspace_identity_fails_before_creating_files(
    tmp_path: Path, ticket: str, round_id: str
) -> None:
    with pytest.raises(CwError, match="invalid review workspace"):
        initialize_workspace(
            tmp_path, ticket=ticket, round_id=round_id, roles=["Code Reviewer"]
        )

    assert not (tmp_path / ".cw").exists()


def test_empty_or_duplicate_rosters_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(CwError, match="at least one role"):
        initialize_workspace(tmp_path, ticket="T-1", round_id="r", roles=[])
    with pytest.raises(CwError, match="duplicate roles"):
        initialize_workspace(
            tmp_path, ticket="T-1", round_id="r", roles=["Reviewer", "Reviewer"]
        )
    assert not (tmp_path / ".cw").exists()


@pytest.mark.parametrize("role", ["", "role with spaces?", "Rôle"])
def test_invalid_reviewer_role_fails_closed(tmp_path: Path, role: str) -> None:
    with pytest.raises(CwError, match="invalid reviewer role"):
        initialize_workspace(tmp_path, ticket="T-1", round_id="round-1", roles=[role])

    assert not (tmp_path / ".cw").exists()


def test_reviewer_role_slug_collisions_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(CwError, match="colliding role slugs"):
        initialize_workspace(
            tmp_path,
            ticket="T-1",
            round_id="round-1",
            roles=["SysAdmin / Scope", "SysAdmin-Scope"],
        )


def test_cw_directory_symlink_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / ".cw").symlink_to(target, target_is_directory=True)

    with pytest.raises(CwError, match="must not be a symlink"):
        initialize_workspace(
            tmp_path, ticket="T-1", round_id="round-1", roles=["Reviewer"]
        )


@pytest.mark.parametrize("lock_kind", ["directory", "symlink"])
def test_invalid_workspace_lock_object_fails_closed(
    tmp_path: Path, lock_kind: str
) -> None:
    initialize_workspace(tmp_path, ticket="T-1", round_id="round-1", roles=["Reviewer"])
    lock = tmp_path / ".cw/.review-findings.lock"
    lock.unlink(missing_ok=True)
    if lock_kind == "directory":
        lock.mkdir()
    else:
        target = tmp_path / "lock-target"
        target.write_text("")
        lock.symlink_to(target)

    with pytest.raises(CwError, match="lock is not a regular file"):
        reconcile_workspace(tmp_path)


def test_workspace_setup_and_lock_system_errors_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_mkdir = Path.mkdir

    def fail_workspace_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == tmp_path / ".cw":
            message = "read-only"
            raise PermissionError(message)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_workspace_mkdir)
    with pytest.raises(CwError, match="cannot create review workspace directory"):
        initialize_workspace(
            tmp_path, ticket="T-1", round_id="round-1", roles=["Reviewer"]
        )
    monkeypatch.setattr(Path, "mkdir", original_mkdir)

    initialize_workspace(tmp_path, ticket="T-1", round_id="round-1", roles=["Reviewer"])

    def fail_flock(*_args: object) -> None:
        message = "lock failure"
        raise OSError(message)

    monkeypatch.setattr(_lock.fcntl, "flock", fail_flock)
    with pytest.raises(CwError, match="cannot lock review workspace"):
        reconcile_workspace(tmp_path)


def test_workspace_setup_detects_non_directory_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_is_dir = Path.is_dir

    def report_workspace_not_directory(path: Path) -> bool:
        if path == tmp_path / ".cw":
            return False
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", report_workspace_not_directory)
    with pytest.raises(CwError, match="workspace path is not a directory"):
        initialize_workspace(
            tmp_path, ticket="T-1", round_id="round-1", roles=["Reviewer"]
        )


def test_non_regular_files_and_atomic_write_failures_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "not-object.json"
    marker.write_text("[]")
    with pytest.raises(CwError, match="must contain a JSON object"):
        workspace_module._read_object(marker, "test object")

    os.mkfifo(tmp_path / "special")
    assert workspace_module._kind(tmp_path / "special") == "other"

    def fail_atomic_write(*_args: object, **_kwargs: object) -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(_primitives, "atomic_write_text", fail_atomic_write)
    with pytest.raises(CwError, match="cannot atomically write test object"):
        workspace_module._write_object(tmp_path / "output.json", {}, "test object")


def test_reconcile_resets_missing_result_and_keeps_friction(tmp_path: Path) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    record_failure(
        tmp_path,
        role="Code Reviewer",
        failure=ReviewerRunFailure(role="Code Reviewer", reason="truncated_response"),
    )
    marker_path = tmp_path / ".cw/review-findings-round.json"
    marker = json.loads(marker_path.read_text())
    marker["SPAWNED_ROLES"] = [{"role": "Code Reviewer", "status": "completed"}]
    marker["completed_roles"] = ["Code Reviewer"]
    marker["failed_reviewers"] = []
    marker_path.write_text(json.dumps(marker))

    result = reconcile_workspace(tmp_path)

    assert result.pending_roles == ("Code Reviewer",)
    updated = _marker(tmp_path)
    assert any(
        "reviewer_failed: Code Reviewer" in note
        for note in updated["friction_highlights"]
    )
    assert (tmp_path / ".cw/review-findings-round.json").is_file()


def test_invalid_artifact_fails_closed_and_remains_for_recovery(tmp_path: Path) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    artifact = tmp_path / ".cw/review-findings/code-reviewer.json"
    artifact.write_text("{not json")

    with pytest.raises(CwError, match="cannot read reviewer findings artifact"):
        reconcile_workspace(tmp_path)

    assert artifact.read_text() == "{not json"
    result = CliRunner().invoke(
        main,
        ["review", "workspace", "reconcile", "--worktree", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "invalid" in result.stderr.lower() or "cannot read" in result.stderr.lower()


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("code-reviewer.json", _document("Other"), "names role 'Other'"),
        (
            "code-reviewer.json",
            _document("Code Reviewer", status="failed"),
            "must be stored as a failure record",
        ),
        ("notes.txt", "partial", "unexpected artifact"),
        ("unexpected.json", _document("Other"), "outside the reviewer roster"),
    ],
)
def test_reconcile_rejects_present_inconsistent_artifacts(
    tmp_path: Path, name: str, content: str, message: str
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    artifact = tmp_path / ".cw/review-findings" / name
    artifact.write_text(content)

    with pytest.raises(CwError, match=message):
        reconcile_workspace(tmp_path)

    assert artifact.is_file()


@pytest.mark.parametrize(
    ("role", "content", "message"),
    [
        ("Outside", _document("Outside"), "outside the persisted roster"),
        ("Code Reviewer", "not json", "invalid reviewer findings document"),
        ("Code Reviewer", _document("Other"), "does not match role"),
        (
            "Code Reviewer",
            _document("Code Reviewer", status="failed"),
            "does not match role",
        ),
    ],
)
def test_record_document_rejects_invalid_returns_without_writing(
    tmp_path: Path, role: str, content: str, message: str
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )

    with pytest.raises(CwError, match=message):
        record_document(tmp_path, role=role, content=content)

    assert not (tmp_path / ".cw/review-findings/code-reviewer.json").exists()
    assert _marker(tmp_path)["SPAWNED_ROLES"][0]["status"] == "pending"


def test_record_failure_checks_role_and_completed_round_precedence(
    tmp_path: Path,
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    with pytest.raises(CwError, match="names 'Other'"):
        record_failure(
            tmp_path,
            role="Code Reviewer",
            failure=ReviewerRunFailure(role="Other", reason="timeout"),
        )
    record_document(tmp_path, role="Code Reviewer", content=_document("Code Reviewer"))
    with pytest.raises(CwError, match="already completed"):
        record_failure(
            tmp_path,
            role="Code Reviewer",
            failure=ReviewerRunFailure(role="Code Reviewer", reason="timeout"),
        )


def test_resume_rejects_wrong_ticket_and_reconcile_requires_active_round(
    tmp_path: Path,
) -> None:
    with pytest.raises(CwError, match="no active review workspace"):
        reconcile_workspace(tmp_path)
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    with pytest.raises(CwError, match="belongs to ticket 'T-1'"):
        initialize_workspace(
            tmp_path, ticket="T-2", round_id="round-2", roles=["Other"]
        )

    shutil.rmtree(tmp_path / ".cw/review-findings")
    with pytest.raises(CwError, match="findings workspace is not a directory"):
        reconcile_workspace(tmp_path)


def test_terminal_finish_is_idempotent_but_cannot_change_status(
    tmp_path: Path,
) -> None:
    _seed_completed(tmp_path)

    assert finish_workspace(tmp_path, status="failed").failed_reviewers
    with pytest.raises(CwError, match="already terminal"):
        finish_workspace(tmp_path, status="completed")


def test_round_identity_is_never_reused(tmp_path: Path) -> None:
    _seed_completed(tmp_path)

    with pytest.raises(CwError, match="archive identity already exists"):
        initialize_workspace(
            tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
        )


def test_failed_staging_preparation_leaves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_marker(*_args: object, **_kwargs: object) -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(_rounds, "_write_marker", fail_marker)

    with pytest.raises(CwError, match="cannot prepare review workspace transaction"):
        initialize_workspace(
            tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
        )

    staging = tmp_path / ".cw/review-findings-staging/T-1-round-1"
    assert (staging / "review-findings").is_dir()
    assert not (staging / "review-findings-round.json").exists()


def test_findings_failure_conflict_and_unexpected_role_fail_closed(
    tmp_path: Path,
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    record_failure(
        tmp_path,
        role="Code Reviewer",
        failure=ReviewerRunFailure(role="Code Reviewer", reason="timeout"),
    )
    artifact = tmp_path / ".cw/review-findings/code-reviewer.json"
    artifact.write_text(_document("Code Reviewer"))
    with pytest.raises(CwError, match="both findings and failure"):
        reconcile_workspace(tmp_path)
    assert artifact.is_file()

    marker = _marker(tmp_path)
    marker["failed_reviewers"] = []
    (tmp_path / ".cw/review-findings-round.json").write_text(json.dumps(marker))
    unexpected = tmp_path / ".cw/review-findings/extra-reviewer.json"
    unexpected.write_text(_document("Extra Reviewer"))
    with pytest.raises(CwError, match="outside the reviewer roster"):
        reconcile_workspace(tmp_path)
    assert unexpected.is_file()


def test_incomplete_and_degraded_reviewers_cannot_finish_successfully(
    tmp_path: Path,
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    with pytest.raises(CwError, match="pending reviewers"):
        finish_workspace(tmp_path, status="completed")
    record_document(
        tmp_path,
        role="Code Reviewer",
        content=_document("Code Reviewer", status="degraded"),
    )
    with pytest.raises(CwError, match="degraded"):
        finish_workspace(tmp_path, status="completed")
    failed = finish_workspace(tmp_path, status="failed")
    assert _marker(tmp_path)["status"] == "failed"
    assert failed.completed_roles == ("Code Reviewer",)


def test_completed_marker_with_degraded_artifact_fails_closed(
    tmp_path: Path,
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    record_document(
        tmp_path,
        role="Code Reviewer",
        content=_document("Code Reviewer", status="degraded"),
    )
    marker_path = tmp_path / ".cw/review-findings-round.json"
    marker = _marker(tmp_path)
    marker["status"] = "completed"
    marker_path.write_text(json.dumps(marker))

    runner = CliRunner()
    reconcile = runner.invoke(
        main,
        ["review", "workspace", "reconcile", "--worktree", str(tmp_path)],
    )
    finish = runner.invoke(
        main,
        [
            "review",
            "workspace",
            "finish",
            "--status",
            "completed",
            "--worktree",
            str(tmp_path),
        ],
    )

    for result in (reconcile, finish):
        assert result.exit_code != 0
        assert "completed review round contains a degraded reviewer" in result.stderr
    assert _marker(tmp_path)["status"] == "completed"
    assert (tmp_path / ".cw/review-findings/code-reviewer.json").is_file()


def test_failed_reviewer_cli_cannot_be_reported_as_successful(tmp_path: Path) -> None:
    runner = CliRunner()
    initialized = runner.invoke(
        main,
        [
            "review",
            "workspace",
            "init",
            "--ticket",
            "T-1",
            "--round-id",
            "round-1",
            "--role",
            "Code Reviewer",
            "--worktree",
            str(tmp_path),
        ],
    )
    assert initialized.exit_code == 0, initialized.stderr
    failure = runner.invoke(
        main,
        [
            "review",
            "workspace",
            "record-failure",
            "Code Reviewer",
            "--reason",
            "timeout",
            "--worktree",
            str(tmp_path),
        ],
    )
    assert failure.exit_code == 0, failure.stderr

    successful_finish = runner.invoke(
        main,
        [
            "review",
            "workspace",
            "finish",
            "--status",
            "completed",
            "--worktree",
            str(tmp_path),
        ],
    )
    assert successful_finish.exit_code != 0
    assert "cannot mark review completed" in successful_finish.stderr
    assert _marker(tmp_path)["status"] == "in_progress"

    failed_finish = runner.invoke(
        main,
        [
            "review",
            "workspace",
            "finish",
            "--status",
            "failed",
            "--worktree",
            str(tmp_path),
        ],
    )
    assert failed_finish.exit_code == 0, failed_finish.stderr
    result = json.loads(failed_finish.stdout)
    assert result["failed_reviewers"][0]["role"] == "Code Reviewer"
    assert _marker(tmp_path)["status"] == "failed"


def test_new_round_archives_documents_failures_and_friction(tmp_path: Path) -> None:
    _seed_completed(tmp_path)

    started = initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-2", roles=["Code Reviewer"]
    )

    archive = tmp_path / ".cw/review-findings-archive/T-1-round-2"
    assert started.action == "started"
    assert (archive / "review-findings/code-reviewer.json").is_file()
    archived = json.loads((archive / "review-findings-round.json").read_text())
    assert archived["failed_reviewers"][0]["reason"] == "timeout"
    assert any(
        "reviewer_failed: Security" in note for note in archived["friction_highlights"]
    )
    assert _marker(tmp_path)["round_id"] == "round-2"


@pytest.mark.parametrize("previous", [False, True], ids=["first-run", "archived"])
def test_recovery_promotes_verified_orphan_staging_round(
    tmp_path: Path, previous: bool
) -> None:
    if previous:
        _seed_completed(tmp_path)
    staging = _write_staging_round(tmp_path, ticket="T-2", round_id="round-2")

    recover_workspace(tmp_path)
    recover_workspace(tmp_path)

    assert _marker(tmp_path)["round_id"] == "round-2"
    assert not (staging / "review-findings-round.json").exists()
    if previous:
        archive = tmp_path / ".cw/review-findings-archive/T-2-round-2"
        assert (archive / "review-findings/code-reviewer.json").is_file()
    else:
        archive = tmp_path / ".cw/review-findings-archive/T-2-round-2"
        assert (archive / "archive-transaction.json").is_file()
        assert not (archive / "review-findings-round.json").exists()


def test_orphan_staging_over_in_progress_round_fails_closed(tmp_path: Path) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    staging = _write_staging_round(tmp_path, ticket="T-2", round_id="round-2")

    with pytest.raises(CwError, match="over an in-progress active round"):
        recover_workspace(tmp_path)

    assert _marker(tmp_path)["round_id"] == "round-1"
    assert (staging / "review-findings-round.json").is_file()


def test_orphan_staging_with_existing_active_marker_only_fails_closed(
    tmp_path: Path,
) -> None:
    cw = tmp_path / ".cw"
    cw.mkdir()
    (cw / "review-findings-round.json").write_text(
        json.dumps(
            {
                "ticket": "T-1",
                "round_id": "round-1",
                "status": "failed",
                "SPAWNED_ROLES": ["Reviewer"],
            }
        )
    )
    staging = _write_staging_round(tmp_path, ticket="T-2", round_id="round-2")

    with pytest.raises(CwError, match="inconsistent active pair"):
        recover_workspace(tmp_path)

    assert (cw / "review-findings-round.json").is_file()
    assert (staging / "review-findings-round.json").is_file()


def test_orphan_staging_results_must_be_empty_before_promotion(
    tmp_path: Path,
) -> None:
    staging = _write_staging_round(tmp_path, ticket="T-1", round_id="round-1")
    marker_path = staging / "review-findings-round.json"
    marker = workspace_module._change_marker(
        workspace_module._parse_marker(marker_path),
        roles={"Code Reviewer": "completed"},
    )
    workspace_module._write_marker(marker_path, marker)
    artifact = staging / "review-findings/code-reviewer.json"
    artifact.write_text(_document("Code Reviewer"))

    with pytest.raises(CwError, match="already has reviewer artifacts"):
        recover_workspace(tmp_path)

    assert artifact.is_file()


def test_staging_identity_must_match_its_marker(tmp_path: Path) -> None:
    staging = _write_staging_round(tmp_path, ticket="T-1", round_id="round-1")
    renamed = staging.with_name("unexpected-name")
    staging.rename(renamed)

    with pytest.raises(CwError, match="staging path does not match marker identity"):
        recover_workspace(tmp_path)

    assert (renamed / "review-findings-round.json").is_file()


def test_staging_without_marker_fails_closed_and_is_preserved(tmp_path: Path) -> None:
    staging = tmp_path / ".cw/review-findings-staging/unidentified"
    staging.mkdir(parents=True)

    with pytest.raises(CwError, match="staging bundle has no valid marker"):
        recover_workspace(tmp_path)

    assert staging.is_dir()


@pytest.mark.parametrize("entry_kind", ["file", "empty-transaction", "legacy-archive"])
def test_unrecognized_archive_entries_fail_closed(
    tmp_path: Path, entry_kind: str
) -> None:
    archive_root = tmp_path / ".cw/review-findings-archive"
    archive_root.mkdir(parents=True)
    if entry_kind == "file":
        (archive_root / "unexpected").write_text("preserve")
        message = "invalid entry in review archive root"
    else:
        old_round = archive_root / "T-1-round-1"
        old_round.mkdir()
        if entry_kind == "legacy-archive":
            (old_round / "review-findings-round.json").write_text(
                json.dumps(
                    {
                        "ticket": "T-1",
                        "round_id": "round-1",
                        "status": "failed",
                        "SPAWNED_ROLES": ["Reviewer"],
                    }
                )
            )
            (old_round / "review-findings").mkdir()
            message = "no valid manifest"
        else:
            message = "manifest is missing"

    with pytest.raises(CwError, match=message):
        recover_workspace(tmp_path)

    assert list(archive_root.iterdir())


def test_multiple_pending_transactions_fail_closed(tmp_path: Path) -> None:
    for ticket, round_id in (("T-1", "round-1"), ("T-2", "round-2")):
        _write_staging_round(tmp_path, ticket=ticket, round_id=round_id)
        transaction = workspace_module._new_transaction(ticket, round_id, previous=None)
        transaction_dir = (
            tmp_path / ".cw/review-findings-archive" / f"{ticket}-{round_id}"
        )
        transaction_dir.mkdir(parents=True)
        workspace_module._write_transaction(tmp_path, transaction)

    with pytest.raises(
        CwError, match="multiple incomplete review archive transactions"
    ):
        recover_workspace(tmp_path)

    assert len(list((tmp_path / ".cw/review-findings-staging").iterdir())) == 2


def test_multiple_orphan_staging_bundles_fail_closed(tmp_path: Path) -> None:
    first = _write_staging_round(tmp_path, ticket="T-1", round_id="round-1")
    second = _write_staging_round(tmp_path, ticket="T-2", round_id="round-2")

    with pytest.raises(CwError, match="multiple orphan review staging bundles"):
        recover_workspace(tmp_path)

    assert (first / "review-findings-round.json").is_file()
    assert (second / "review-findings-round.json").is_file()


def _operations_for_transition(root: Path, *, previous: bool) -> list[str]:
    if previous:
        _seed_completed(root)
    observed: list[str] = []
    original_rename = workspace_module._rename
    original_write = workspace_module._write_transaction

    def observe_rename(source: Path, destination: Path) -> None:
        original_rename(source, destination)
        observed.append("rename")

    def observe_write(
        worktree: Path, transaction: workspace_module._Transaction
    ) -> None:
        original_write(worktree, transaction)
        observed.append("phase")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_recovery, "_rename", observe_rename)
        patch.setattr(_recovery, "_write_transaction", observe_write)
        patch.setattr(_rounds, "_write_transaction", observe_write)
        initialize_workspace(
            root, ticket="T-2", round_id="round-2", roles=["Code Reviewer"]
        )
    return observed


@pytest.mark.parametrize("previous", [False, True], ids=["first-run", "archived-round"])
def test_recovery_is_idempotent_after_each_rename_and_phase_write(
    tmp_path: Path, previous: bool
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    operations = _operations_for_transition(baseline, previous=previous)
    assert "rename" in operations
    assert "phase" in operations

    for interrupt_at in range(len(operations)):
        root = tmp_path / f"case-{interrupt_at}"
        root.mkdir()
        if previous:
            _seed_completed(root)
        original_rename = workspace_module._rename
        original_write = workspace_module._write_transaction
        calls = 0

        def interrupt_after(
            operation: str,
            call: Callable[[], T],
            target: int = interrupt_at,
        ) -> T:
            nonlocal calls
            result = call()
            current = calls
            calls += 1
            if current == target:
                message = f"interrupted after {operation} {current}"
                raise RuntimeError(message)
            return result

        def interrupted_rename(
            source: Path,
            destination: Path,
            rename: Callable[[Path, Path], None] = original_rename,
        ) -> None:
            interrupt_after("rename", lambda: rename(source, destination))

        def interrupted_write(
            worktree: Path,
            transaction: workspace_module._Transaction,
            write: Callable[[Path, workspace_module._Transaction], None] = (
                original_write
            ),
        ) -> None:
            interrupt_after("phase write", lambda: write(worktree, transaction))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(_recovery, "_rename", interrupted_rename)
            patch.setattr(_recovery, "_write_transaction", interrupted_write)
            patch.setattr(_rounds, "_write_transaction", interrupted_write)
            with pytest.raises(RuntimeError, match="interrupted"):
                initialize_workspace(
                    root, ticket="T-2", round_id="round-2", roles=["Code Reviewer"]
                )

        recover_workspace(root)
        recover_workspace(root)
        assert _marker(root)["round_id"] == "round-2"
        staging = root / ".cw/review-findings-staging/T-2-round-2"
        assert sorted(path.name for path in staging.iterdir()) == []
        if previous:
            archive = root / ".cw/review-findings-archive/T-2-round-2"
            archived = json.loads((archive / "review-findings-round.json").read_text())
            assert archived["round_id"] == "round-1"
            assert (archive / "review-findings/code-reviewer.json").is_file()
            assert archived["failed_reviewers"][0]["role"] == "Security"


def test_invalid_transaction_path_fails_closed_without_moving_evidence(
    tmp_path: Path,
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    path = tmp_path / ".cw/review-findings-archive/T-1-round-1/archive-transaction.json"
    transaction = json.loads(path.read_text())
    transaction["archive_marker"] = "../../outside.json"
    path.write_text(json.dumps(transaction))

    with pytest.raises(CwError, match="invalid archive_marker path"):
        recover_workspace(tmp_path)

    assert path.is_file()
    assert (tmp_path / ".cw/review-findings").is_dir()
    assert (tmp_path / ".cw/review-findings-round.json").is_file()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload.update(extra="unknown"),
            "invalid archive transaction manifest",
        ),
        (
            lambda payload: payload.update(
                has_old_round=True,
                previous_ticket="T-old",
                previous_round_id=None,
            ),
            "inconsistent previous identity",
        ),
        (
            lambda payload: payload.update(phase="unknown"),
            "invalid archive transaction manifest",
        ),
        (
            lambda payload: payload.update(ticket="T-2"),
            "invalid archive_directory path",
        ),
    ],
    ids=[
        "unknown-field",
        "partial-previous-identity",
        "invalid-phase",
        "identity-path",
    ],
)
def test_invalid_transaction_manifest_fails_closed(
    tmp_path: Path, mutation: Callable[[dict[str, object]], None], message: str
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    manifest = (
        tmp_path / ".cw/review-findings-archive/T-1-round-1/archive-transaction.json"
    )
    payload = json.loads(manifest.read_text())
    mutation(payload)
    manifest.write_text(json.dumps(payload))

    with pytest.raises(CwError, match=message):
        recover_workspace(tmp_path)

    assert manifest.is_file()
    assert (tmp_path / ".cw/review-findings/code-reviewer.json").parent.is_dir()


def test_transaction_manifest_must_match_its_archive_identity_path(
    tmp_path: Path,
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    original = (
        tmp_path / ".cw/review-findings-archive/T-1-round-1/archive-transaction.json"
    )
    misplaced = (
        tmp_path / ".cw/review-findings-archive/T-2-round-2/archive-transaction.json"
    )
    misplaced.parent.mkdir()
    misplaced.write_text(original.read_text())

    with pytest.raises(CwError, match="identity does not match its path"):
        recover_workspace(tmp_path)

    assert original.is_file()
    assert misplaced.is_file()


def test_committed_transaction_rejects_symlinked_paths(tmp_path: Path) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    staging = tmp_path / ".cw/review-findings-staging/T-1-round-1/review-findings"
    staging.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(CwError, match="expected 'absent'"):
        recover_workspace(tmp_path)

    assert staging.is_symlink()


def test_archive_and_staging_roots_must_be_real_directories(tmp_path: Path) -> None:
    cw = tmp_path / ".cw"
    cw.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (cw / "review-findings-archive").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CwError, match="transaction root is not a real directory"):
        recover_workspace(tmp_path)

    assert list(outside.iterdir()) == []


def test_inconsistent_first_run_phase_fails_without_moving_files(
    tmp_path: Path,
) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    manifest = (
        tmp_path / ".cw/review-findings-archive/T-1-round-1/archive-transaction.json"
    )
    payload = json.loads(manifest.read_text())
    payload["phase"] = "prepared"
    manifest.write_text(json.dumps(payload))

    with pytest.raises(CwError, match="inconsistent with committed files"):
        recover_workspace(tmp_path)

    assert (tmp_path / ".cw/review-findings-round.json").is_file()
    assert manifest.is_file()


def test_workspace_lock_reports_contention(tmp_path: Path) -> None:
    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-1", roles=["Code Reviewer"]
    )
    errors: list[CwError] = []
    attempted = threading.Event()
    finished = threading.Event()

    def mutate() -> None:
        attempted.set()
        try:
            reconcile_workspace(tmp_path)
        except CwError as exc:
            errors.append(exc)
        finally:
            finished.set()

    with workspace_module._workspace_lock(tmp_path):
        thread = threading.Thread(target=mutate)
        thread.start()
        assert attempted.wait(timeout=2)
        assert finished.wait(timeout=2)
        thread.join(timeout=2)

    assert len(errors) == 1
    assert "locked by another process" in str(errors[0])


def test_transaction_rename_rejects_invalid_paths_and_os_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    destination = tmp_path / "destination"
    with pytest.raises(CwError, match="source is missing or invalid"):
        workspace_module._rename(missing, destination)

    source = tmp_path / "source"
    source.write_text("preserve")
    destination.write_text("occupied")
    with pytest.raises(CwError, match="destination already exists"):
        workspace_module._rename(source, destination)

    destination.unlink()

    def fail_rename(_source: Path, _destination: Path) -> None:
        message = "cross-device"
        raise OSError(message)

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(CwError, match="cannot rename"):
        workspace_module._rename(source, destination)
    assert source.read_text() == "preserve"
    assert not destination.exists()


def _reprompt_round(root: Path) -> None:
    initialize_workspace(
        root, ticket="T-1", round_id="round-1", roles=["Code Reviewer", "Security"]
    )


def test_record_reprompt_grants_once_without_consuming_role(tmp_path: Path) -> None:
    _reprompt_round(tmp_path)

    _result, granted = record_reprompt(tmp_path, role="Code Reviewer")

    assert granted is True
    marker = _marker(tmp_path)
    assert marker["reprompted_roles"] == ["Code Reviewer"]
    assert "reviewer_reprompted: Code Reviewer (frame_missing)" in json.dumps(
        marker["friction_highlights"]
    )
    assert _result.pending_roles == ("Code Reviewer", "Security")


def test_record_reprompt_second_call_refused_with_no_mutation(tmp_path: Path) -> None:
    _reprompt_round(tmp_path)
    record_reprompt(tmp_path, role="Code Reviewer")
    before = (tmp_path / ".cw/review-findings-round.json").read_bytes()

    _result, granted = record_reprompt(tmp_path, role="Code Reviewer")

    assert granted is False
    assert (tmp_path / ".cw/review-findings-round.json").read_bytes() == before


def test_record_reprompt_grant_survives_resume(tmp_path: Path) -> None:
    _reprompt_round(tmp_path)
    record_reprompt(tmp_path, role="Code Reviewer")

    initialize_workspace(
        tmp_path, ticket="T-1", round_id="round-2", roles=["Different Roster"]
    )
    _result, granted = record_reprompt(tmp_path, role="Code Reviewer")

    assert granted is False
    assert _marker(tmp_path)["reprompted_roles"] == ["Code Reviewer"]


def test_record_reprompt_rejects_role_outside_roster(tmp_path: Path) -> None:
    _reprompt_round(tmp_path)

    with pytest.raises(CwError, match="outside the persisted roster"):
        record_reprompt(tmp_path, role="Not A Reviewer")


def test_record_reprompt_rejects_completed_role(tmp_path: Path) -> None:
    _reprompt_round(tmp_path)
    record_document(tmp_path, role="Code Reviewer", content=_document("Code Reviewer"))

    with pytest.raises(CwError, match="already completed"):
        record_reprompt(tmp_path, role="Code Reviewer")


def test_record_reprompt_rejects_terminal_round(tmp_path: Path) -> None:
    _reprompt_round(tmp_path)
    record_document(tmp_path, role="Code Reviewer", content=_document("Code Reviewer"))
    record_failure(
        tmp_path,
        role="Security",
        failure=ReviewerRunFailure(role="Security", reason="timeout"),
    )
    finish_workspace(tmp_path, status="failed")

    with pytest.raises(CwError, match="cannot record a reviewer return"):
        record_reprompt(tmp_path, role="Security")


def test_record_reprompt_then_failure_and_finish_still_work(tmp_path: Path) -> None:
    _reprompt_round(tmp_path)
    record_reprompt(tmp_path, role="Security")
    record_failure(
        tmp_path,
        role="Security",
        failure=ReviewerRunFailure(
            role="Security", reason="unparseable_response", detail="no frame"
        ),
    )
    record_document(tmp_path, role="Code Reviewer", content=_document("Code Reviewer"))

    finished = finish_workspace(tmp_path, status="failed")

    assert finished.completed_roles == ("Code Reviewer",)


@pytest.mark.parametrize(
    ("reprompted", "message"),
    [
        ("Code Reviewer", "reprompted_roles must be a list of strings"),
        ([1], "reprompted_roles must be a list of strings"),
        (["Code Reviewer", "Code Reviewer"], "repeats a re-prompted role"),
        (["Ghost"], "outside roster"),
    ],
    ids=["non-list", "non-string", "duplicate", "outside-roster"],
)
def test_reprompted_roles_fail_closed_on_corrupt_marker(
    tmp_path: Path, reprompted: object, message: str
) -> None:
    _reprompt_round(tmp_path)
    payload = _marker(tmp_path)
    payload["reprompted_roles"] = reprompted
    _write_marker(tmp_path, payload)

    with pytest.raises(CwError, match=message):
        record_reprompt(tmp_path, role="Code Reviewer")


def test_reprompt_grant_on_marker_without_prior_key(tmp_path: Path) -> None:
    _reprompt_round(tmp_path)
    assert "reprompted_roles" not in _marker(tmp_path)

    _result, granted = record_reprompt(tmp_path, role="Security")

    assert granted is True
    assert _marker(tmp_path)["reprompted_roles"] == ["Security"]
