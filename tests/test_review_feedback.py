"""Strict review-feedback payload and queue-row compatibility tests."""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from cw.dev_queue import migrate_dev_queue
from cw.models import DEV_QUEUE_SCHEMA_VERSION, DevQueueStore, TicketTask
from cw.models.review_feedback import MAX_REVIEW_REPORT_BYTES, ReviewFeedback
from tests._shared.workflow_text import _COMMANDS_ROOT, _REPO_ROOT, _iter_src_files

# Built from two literals so a repo-wide `git grep -i` of tests/ is also clean.
_TERM = "pi" + "ta"
_TERM_RE = re.compile(rf"(?<![A-Za-z]){_TERM}(?![A-Za-z])", re.IGNORECASE)
_REVIEWED_SHA = "0123456789abcdef0123456789abcdef01234567"
_CAPTURED_AT = "2026-11-04T12:30:00Z"
_REPORT = "Review report:\n\n- Verify the observed behavior.\n\n"


def test_downstream_review_skill_name_is_absent_from_shipped_surfaces() -> None:
    files = sorted(
        [
            *_iter_src_files(),
            *(_REPO_ROOT / "src/cw/data").rglob("*.md"),
            *_COMMANDS_ROOT.rglob("*.md"),
            *(_REPO_ROOT / "docs").rglob("*.md"),
        ]
    )
    # Floor: the scan cannot pass vacuously.
    assert {"auto-dev-impl.md", "dispatch-runbook.md", "review_feedback.py"} <= {
        p.name for p in files
    }
    hits = [
        f"{p.relative_to(_REPO_ROOT)}:{n}: {line.strip()}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _TERM_RE.search(line)
    ]
    assert hits == []


def test_term_matcher_is_whole_token_only() -> None:
    assert _TERM_RE.search(f"{_TERM.upper()} report")
    assert _TERM_RE.search(f"test_runbook_{_TERM}_handoff")
    assert not _TERM_RE.search(f"hos{_TERM}al")
    assert not _TERM_RE.search(f"ca{_TERM}l")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "repo": "octo/project",
        "pr_number": 42,
        "reviewed_sha": _REVIEWED_SHA,
        "captured_at": _CAPTURED_AT,
        "report": _REPORT,
    }
    payload.update(overrides)
    return payload


def test_review_feedback_round_trips_without_changing_report_text() -> None:
    feedback = ReviewFeedback.model_validate_json(json.dumps(_payload()))

    assert feedback.repo == "octo/project"
    assert feedback.pr_number == 42
    assert feedback.reviewed_sha == _REVIEWED_SHA
    assert feedback.report == _REPORT
    assert ReviewFeedback.model_validate_json(feedback.model_dump_json()) == feedback


@pytest.mark.parametrize(
    "overrides",
    [
        {"repo": "octo"},
        {"repo": "/project"},
        {"repo": "octo//project"},
        {"repo": "owner/.."},
        {"repo": "https://github.com/octo/project"},
        {"repo": "octo/project/extra"},
        {"pr_number": 0},
        {"pr_number": -1},
        {"pr_number": "42"},
        {"pr_number": True},
        {"reviewed_sha": "deadbee"},
        {"reviewed_sha": "g" * 40},
        {"captured_at": "2026-11-04T12:30:00"},
        {"captured_at": "not a timestamp"},
        {"captured_at": 1793795400},
        {"report": ""},
        {"report": " \n\t"},
        {"extra": "must be rejected"},
    ],
)
def test_review_feedback_rejects_invalid_or_unknown_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ReviewFeedback.model_validate_json(json.dumps(_payload(**overrides)))


@pytest.mark.parametrize("character", ["x", "é", "😀"])
def test_review_feedback_rejects_report_over_byte_limit(character: str) -> None:
    report = character * (MAX_REVIEW_REPORT_BYTES // len(character.encode("utf-8")) + 1)
    assert len(report.encode("utf-8")) > MAX_REVIEW_REPORT_BYTES
    if character != "x":
        assert len(report) < MAX_REVIEW_REPORT_BYTES

    with pytest.raises(ValidationError, match=r"report must be at most .* UTF-8 bytes"):
        ReviewFeedback.model_validate_json(json.dumps(_payload(report=report)))


@pytest.mark.parametrize("character", ["x", "é", "😀"])
def test_review_feedback_accepts_exact_byte_limit(character: str) -> None:
    report = character * (MAX_REVIEW_REPORT_BYTES // len(character.encode("utf-8")))
    assert len(report.encode("utf-8")) == MAX_REVIEW_REPORT_BYTES

    feedback = ReviewFeedback.model_validate_json(json.dumps(_payload(report=report)))

    assert feedback.report == report


def test_legacy_queue_row_without_feedback_loads_with_none() -> None:
    row = TicketTask.model_validate({"ticket_id": "T-1", "client": "test-client"})

    assert row.review_feedback is None


def test_pre_v51_queue_data_migrates_without_feedback_filler() -> None:
    legacy = {
        "schema_version": 50,
        "tasks": [{"ticket_id": "T-50", "client": "test-client"}],
    }

    migrated = migrate_dev_queue(legacy)
    (row,) = DevQueueStore.model_validate(migrated).tasks

    assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION
    assert row.review_feedback is None


def test_dev_queue_schema_version_includes_review_feedback() -> None:
    assert DEV_QUEUE_SCHEMA_VERSION == 60


def test_pre_v53_row_loads_without_red_pr_fields() -> None:
    legacy = {
        "schema_version": 52,
        "tasks": [{"ticket_id": "T-52", "client": "test-client"}],
    }

    migrated = migrate_dev_queue(legacy)
    (row,) = DevQueueStore.model_validate(migrated).tasks

    assert migrated["schema_version"] == DEV_QUEUE_SCHEMA_VERSION
    assert row.shipped_red is None
    assert row.red_pr_recovery_sha is None
