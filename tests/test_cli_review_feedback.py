"""Operator ingestion and fail-closed validation for review feedback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click import ClickException
from click.testing import CliRunner

from cw.cli import main
from cw.cli.dev_queue.crud import _load_review_feedback
from cw.dev_queue import load_dev_queue
from cw.models import OrchestratorEventType, Stage
from cw.models.review_feedback import MAX_REVIEW_REPORT_BYTES
from tests._clients_yaml import ClientSpec, write_clients_yaml

_FEEDBACK: dict[str, object] = {
    "repo": "octo/project",
    "pr_number": 42,
    "reviewed_sha": "0123456789abcdef0123456789abcdef01234567",
    "captured_at": "2026-11-04T12:30:00Z",
    "report": "Review report:\n- check behavior\n",
}


@pytest.fixture
def recorded_events(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[OrchestratorEventType, dict[str, object]]]:
    events: list[tuple[OrchestratorEventType, dict[str, object]]] = []

    def record_event(
        event_type: OrchestratorEventType, payload: dict[str, object]
    ) -> None:
        events.append((event_type, payload))

    monkeypatch.setattr("cw.dev_queue.crud.record_event", record_event)
    return events


@pytest.fixture
def configured_dev_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_events: list[tuple[OrchestratorEventType, dict[str, object]]],
) -> Path:
    queue_file = tmp_path / "dev_queue.json"
    monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", queue_file)
    monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", tmp_path / ".dev_queue.lock")
    monkeypatch.setattr("cw.config.DEV_PLAN_FILE", tmp_path / "dev_plan.json")
    monkeypatch.setattr("cw.config.DEV_PLAN_LOCK", tmp_path / ".dev_plan.lock")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_clients_yaml(ClientSpec("acme", workspace))
    config_dir = tmp_path / ".claude-workspace"
    config_dir.mkdir()
    monkeypatch.setattr("cw.config.ORCHESTRATOR_CONFIG_DIR", config_dir)
    monkeypatch.setattr(
        "cw.config.ORCHESTRATOR_CONFIG_FILE", config_dir / "orchestrator.yaml"
    )
    monkeypatch.setattr(
        "cw.cli.dev_queue.crud.echo_resulting_row", lambda *_a, **_kw: None
    )
    return tmp_path


def _write_payload(path: Path, payload: object = _FEEDBACK) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _add_args(path: Path, *tail: str) -> list[str]:
    return [
        "dev-queue",
        "add",
        "ACME-42",
        "-c",
        "acme",
        *tail,
        "--review-feedback-file",
        str(path),
    ]


@pytest.mark.parametrize(
    "report",
    [_FEEDBACK["report"], "é" * (MAX_REVIEW_REPORT_BYTES // 2)],
    ids=["ascii", "multibyte-exact-byte-limit"],
)
def test_add_ingests_review_feedback_for_one_impl_task(
    configured_dev_queue: Path,
    recorded_events: list[tuple[OrchestratorEventType, dict[str, object]]],
    report: str,
) -> None:
    payload_file = _write_payload(
        configured_dev_queue / "feedback.json", {**_FEEDBACK, "report": report}
    )

    result = CliRunner().invoke(main, _add_args(payload_file, "--stage", "impl"))

    assert result.exit_code == 0, result.output
    (row,) = load_dev_queue().tasks
    assert row.stage == Stage.IMPL
    assert row.review_feedback is not None
    assert row.review_feedback.repo == "octo/project"
    assert row.review_feedback.pr_number == 42
    assert row.review_feedback.report == report
    assert recorded_events == [
        (
            OrchestratorEventType.TICKET_ENQUEUED,
            {"ticket_id": "ACME-42", "client": "acme", "priority": 0, "stage": "impl"},
        )
    ]


@pytest.mark.parametrize("stage_args", [(), ("--stage", "plan")])
def test_feedback_requires_explicit_impl_stage(
    configured_dev_queue: Path,
    recorded_events: list[tuple[OrchestratorEventType, dict[str, object]]],
    stage_args: tuple[str, ...],
) -> None:
    payload_file = _write_payload(configured_dev_queue / "feedback.json")

    result = CliRunner().invoke(main, _add_args(payload_file, *stage_args))

    assert result.exit_code != 0
    assert "--stage impl" in result.output
    assert load_dev_queue().tasks == []
    assert recorded_events == []


def test_feedback_rejects_multiple_ticket_ids(
    configured_dev_queue: Path,
    recorded_events: list[tuple[OrchestratorEventType, dict[str, object]]],
) -> None:
    payload_file = _write_payload(configured_dev_queue / "feedback.json")
    args = _add_args(payload_file, "--stage", "impl")
    args.insert(3, "ACME-43")

    result = CliRunner().invoke(main, args)

    assert result.exit_code != 0
    assert "exactly one ticket" in result.output
    assert load_dev_queue().tasks == []
    assert recorded_events == []


@pytest.mark.parametrize(
    "contents",
    [
        "{not json",
        json.dumps({**_FEEDBACK, "repo": "octo"}),
        json.dumps({**_FEEDBACK, "reviewed_sha": "short"}),
        json.dumps({**_FEEDBACK, "report": ""}),
        json.dumps({**_FEEDBACK, "report": "x" * (MAX_REVIEW_REPORT_BYTES + 1)}),
        json.dumps({**_FEEDBACK, "report": "é" * (MAX_REVIEW_REPORT_BYTES // 2 + 1)}),
    ],
)
def test_add_rejects_invalid_feedback_without_enqueuing(
    configured_dev_queue: Path,
    recorded_events: list[tuple[OrchestratorEventType, dict[str, object]]],
    contents: str,
) -> None:
    payload_file = configured_dev_queue / "feedback.json"
    payload_file.write_text(contents, encoding="utf-8")

    result = CliRunner().invoke(main, _add_args(payload_file, "--stage", "impl"))

    assert result.exit_code != 0
    assert "Invalid review-feedback file" in result.output
    assert load_dev_queue().tasks == []
    assert recorded_events == []


def test_add_rejects_missing_feedback_file(
    configured_dev_queue: Path,
    recorded_events: list[tuple[OrchestratorEventType, dict[str, object]]],
) -> None:
    missing_file = configured_dev_queue / "missing.json"

    result = CliRunner().invoke(main, _add_args(missing_file, "--stage", "impl"))

    assert result.exit_code != 0
    assert load_dev_queue().tasks == []
    assert recorded_events == []


@pytest.mark.parametrize("entrypoint", ["loader", "cli"])
def test_existing_feedback_file_read_error_has_no_side_effects(
    configured_dev_queue: Path,
    recorded_events: list[tuple[OrchestratorEventType, dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    feedback_file = _write_payload(configured_dev_queue / "feedback.json")
    assert feedback_file.is_file()
    original_read_bytes = Path.read_bytes
    read_attempts: list[Path] = []

    def fail_read(path: Path) -> bytes:
        if path == feedback_file:
            read_attempts.append(path)
            raise PermissionError(13, "Permission denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read)

    expected_error = (
        f"Could not read review-feedback file {feedback_file}: Permission denied"
    )
    if entrypoint == "loader":
        with pytest.raises(ClickException) as exc_info:
            _load_review_feedback(feedback_file)
        assert str(exc_info.value) == expected_error
        assert isinstance(exc_info.value.__cause__, PermissionError)
    else:
        result = CliRunner().invoke(main, _add_args(feedback_file, "--stage", "impl"))
        assert result.exit_code == 1
        assert f"Error: {expected_error}" in result.output

    assert read_attempts == [feedback_file]
    assert load_dev_queue().tasks == []
    assert recorded_events == []
