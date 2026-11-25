"""The saved REVIEW-park blocking findings artifact (#3762), hermetic on tmp."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from cw.models import HOOK_CONTEXT_RELATIVE_PATH, Stage
from cw.models.review_feedback import MAX_REVIEW_REPORT_BYTES
from cw.review_park_findings import (
    REVIEW_PARK_FINDINGS_RELATIVE_PATH,
    ReviewParkFindings,
    clear_park_findings,
    read_park_findings,
    write_park_findings,
)
from cw.stage_verification.posted_text import TRUNCATION_MARKER
from tests._shared.review_builders import regressed_review_row
from tests._shared.stop_hook_context import _write_hook_context_file

_TICKET = "T-3762"
_SHA = "b" * 40
_TEXT = "1. [HIGH] src/a.py\n   summary: boom\n   evidence: line 3"
_LOGGER = "cw.review_park_findings"


def _save(worktree: Path, text: str = _TEXT, ticket_id: str = _TICKET) -> bool:
    return write_park_findings(
        worktree, ticket_id=ticket_id, reviewed_sha=_SHA, text=text
    )


def _artifact(worktree: Path) -> Path:
    return worktree / REVIEW_PARK_FINDINGS_RELATIVE_PATH


class TestPathConstant:
    def test_path_is_the_cw_dir_artifact(self) -> None:
        expected = Path(".cw") / "review-park-findings.json"
        assert expected == REVIEW_PARK_FINDINGS_RELATIVE_PATH


class TestWrite:
    def test_round_trip_creates_cw_dir_and_stamps_ticket_and_sha(
        self, tmp_path: Path
    ) -> None:
        assert _save(tmp_path) is True
        record = json.loads(_artifact(tmp_path).read_text(encoding="utf-8"))
        assert record == {"ticket_id": _TICKET, "reviewed_sha": _SHA, "text": _TEXT}
        assert sorted(p.name for p in (tmp_path / ".cw").iterdir()) == [
            "review-park-findings.json"
        ]

    def test_blank_text_is_refused_and_writes_nothing(self, tmp_path: Path) -> None:
        assert _save(tmp_path, text="  \n \n") is False
        assert not _artifact(tmp_path).exists()

    def test_oserror_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / ".cw").write_text("not a directory", encoding="utf-8")
        assert _save(tmp_path) is False

    def test_secret_is_redacted_and_text_capped(self, tmp_path: Path) -> None:
        secret = "ghp_" + "a" * 36
        assert _save(tmp_path, text=f"token {secret}\n" + "x" * (70 * 1024)) is True
        text = ReviewParkFindings.model_validate_json(
            _artifact(tmp_path).read_text(encoding="utf-8")
        ).text
        assert secret not in text
        assert text.endswith(TRUNCATION_MARKER)
        assert len(text.encode("utf-8")) <= MAX_REVIEW_REPORT_BYTES


class TestRead:
    def test_ok(self, tmp_path: Path) -> None:
        _save(tmp_path)
        read = read_park_findings(tmp_path, _TICKET)
        assert read.refusal is None
        assert read.findings is not None
        assert (read.findings.reviewed_sha, read.findings.text) == (_SHA, _TEXT)

    def test_absent_is_artifact_missing(self, tmp_path: Path) -> None:
        read = read_park_findings(tmp_path, _TICKET)
        assert (read.findings, read.refusal) == (None, "artifact_missing")

    @pytest.mark.parametrize(
        "raw",
        [
            "{not json",
            "[1, 2]",
            json.dumps({"ticket_id": _TICKET, "reviewed_sha": _SHA}),
            json.dumps(
                {"ticket_id": _TICKET, "reviewed_sha": _SHA, "text": "x", "extra": 1}
            ),
            json.dumps({"ticket_id": _TICKET, "reviewed_sha": _SHA, "text": ""}),
            json.dumps({"ticket_id": _TICKET, "reviewed_sha": _SHA, "text": "  \n"}),
        ],
        ids=["bad-json", "wrong-shape", "missing-key", "extra-key", "empty", "blank"],
    )
    def test_invalid_or_blank_is_artifact_missing(
        self, tmp_path: Path, raw: str
    ) -> None:
        (tmp_path / ".cw").mkdir()
        _artifact(tmp_path).write_text(raw, encoding="utf-8")
        read = read_park_findings(tmp_path, _TICKET)
        assert (read.findings, read.refusal) == (None, "artifact_missing")

    def test_undecodable_bytes_are_artifact_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".cw").mkdir()
        _artifact(tmp_path).write_bytes(b"\xff\xfe\x00bad")
        read = read_park_findings(tmp_path, _TICKET)
        assert (read.findings, read.refusal) == (None, "artifact_missing")

    def test_directory_at_the_path_is_artifact_missing(self, tmp_path: Path) -> None:
        _artifact(tmp_path).mkdir(parents=True)
        read = read_park_findings(tmp_path, _TICKET)
        assert (read.findings, read.refusal) == (None, "artifact_missing")

    def test_foreign_ticket_is_not_owned(self, tmp_path: Path) -> None:
        _save(tmp_path, ticket_id="OTHER-1")
        read = read_park_findings(tmp_path, _TICKET)
        assert (read.findings, read.refusal) == (None, "not_owned")


class TestClear:
    def test_missing_file_is_a_silent_no_op(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            clear_park_findings(tmp_path, ticket_id=_TICKET)
        assert caplog.records == []

    def test_existing_file_is_removed(self, tmp_path: Path) -> None:
        _save(tmp_path)
        clear_park_findings(tmp_path, ticket_id=_TICKET)
        assert not _artifact(tmp_path).exists()

    def test_a_directory_at_the_path_is_swallowed(self, tmp_path: Path) -> None:
        _artifact(tmp_path).mkdir(parents=True)
        clear_park_findings(tmp_path, ticket_id=_TICKET)
        assert _artifact(tmp_path).is_dir()


class TestLogging:
    def test_write_failure_logs_one_warning_without_path_or_text(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / ".cw").write_text("not a directory", encoding="utf-8")
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            _save(tmp_path)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert message.startswith("review_park_findings_write_failed")
        assert _TICKET in message
        assert "Error" in message
        self._assert_clean(caplog, tmp_path)

    def test_clear_failure_logs_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _artifact(tmp_path).mkdir(parents=True)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            clear_park_findings(tmp_path, ticket_id=_TICKET)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].getMessage().startswith("review_park_findings_clear_failed")
        self._assert_clean(caplog, tmp_path)

    def test_unreadable_file_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / ".cw").mkdir()
        _artifact(tmp_path).write_text("{" + _TEXT, encoding="utf-8")
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            read_park_findings(tmp_path, _TICKET)
        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debug) == 1
        assert debug[0].getMessage().startswith("review_park_findings_unreadable")
        self._assert_clean(caplog, tmp_path)

    def test_success_paths_log_no_text_or_path(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            _save(tmp_path)
            read_park_findings(tmp_path, _TICKET)
            clear_park_findings(tmp_path, ticket_id=_TICKET)
        names = [r.getMessage().split()[0] for r in caplog.records]
        assert names == [
            "review_park_findings_saved",
            "review_park_findings_cleared",
        ]
        self._assert_clean(caplog, tmp_path)

    def test_blank_text_logs_a_debug_skip(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            _save(tmp_path, text=" ")
        assert [r.getMessage() for r in caplog.records] == [
            f"review_park_findings_skipped ticket={_TICKET} reason=blank_text"
        ]

    @staticmethod
    def _assert_clean(caplog: pytest.LogCaptureFixture, worktree: Path) -> None:
        for record in caplog.records:
            message = record.getMessage()
            assert "boom" not in message
            assert str(worktree) not in message


class TestRegressEndToEnd:
    """Acceptance pin, claude-native surface: the saved park reaches IMPL's context."""

    def test_saved_park_findings_land_in_queue_metadata(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        assert _save(worktree) is True
        _write_hook_context_file(
            worktree,
            client="acme",
            ticket_id=_TICKET,
            session_id="sess-3762",
            task=regressed_review_row(_TICKET, stage_high_water=Stage.REVIEW),
        )
        context = json.loads(
            (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        assert context["queue_metadata"]["regress_review_findings"] == {
            "reviewed_sha": _SHA,
            "text": _TEXT,
        }
