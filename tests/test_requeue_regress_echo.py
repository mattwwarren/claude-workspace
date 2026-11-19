"""The requeue-time REVIEW-regress findings echo (#3694), hermetic.

The echo reads the row and its worktree, then prints one line about whether the
REVIEW verdict's findings reach the IMPL run. Each branch is driven directly so
no dev-queue file or daemon is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from cw.cli.dev_queue import _requeue_echo
from cw.exceptions import CwError
from cw.models import Stage, TicketTask
from cw.models.review_feedback import ReviewFeedback
from tests._shared.review_builders import _make_finding, write_review_verdict_envelope

if TYPE_CHECKING:
    from pathlib import Path

_TICKET = "T-3694"
_SHA = "c" * 40


def _task(**updates: Any) -> TicketTask:
    fields: dict[str, Any] = {
        "ticket_id": _TICKET,
        "client": "test",
        "stage": Stage.IMPL,
        "stage_high_water": Stage.REVIEW,
    }
    fields.update(updates)
    return TicketTask(**fields)


def _patch_row(
    monkeypatch: pytest.MonkeyPatch, *, task: TicketTask | None, worktree: Path | None
) -> None:
    monkeypatch.setattr(_requeue_echo, "load_dev_queue", lambda: None)
    monkeypatch.setattr(_requeue_echo, "_find_ticket", lambda *_a, **_k: task)
    monkeypatch.setattr(_requeue_echo, "get_client", lambda _name: None)
    monkeypatch.setattr(
        _requeue_echo, "resolve_task_worktree", lambda *_a, **_k: worktree
    )


def test_delivered_prints_the_delivered_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_review_verdict_envelope(
        tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, must_fix=[_make_finding()]
    )
    _patch_row(monkeypatch, task=_task(), worktree=tmp_path)
    _requeue_echo._echo_regress_findings(_TICKET, "test")
    out = capsys.readouterr()
    assert f"delivered (reviewed_sha {_SHA})" in out.out
    assert "opencode and local IMPL runs receive nothing" in out.out
    assert out.err == ""


def test_artifact_missing_prints_withheld(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_row(monkeypatch, task=_task(), worktree=tmp_path)
    _requeue_echo._echo_regress_findings(_TICKET, "test")
    assert "withheld (artifact_missing)" in capsys.readouterr().out


def test_non_impl_row_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_row(monkeypatch, task=_task(stage=Stage.REVIEW), worktree=tmp_path)
    _requeue_echo._echo_regress_findings(_TICKET, "test")
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def test_review_feedback_row_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    feedback = ReviewFeedback(
        repo="o/r",
        pr_number=2,
        reviewed_sha=_SHA,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        report="red",
    )
    _patch_row(monkeypatch, task=_task(review_feedback=feedback), worktree=tmp_path)
    _requeue_echo._echo_regress_findings(_TICKET, "test")
    assert capsys.readouterr().out == ""


def test_missing_row_prints_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_row(monkeypatch, task=None, worktree=None)
    _requeue_echo._echo_regress_findings(_TICKET, "test")
    out = capsys.readouterr()
    assert out.out == ""
    assert "could not be determined" in out.err


def test_unresolvable_worktree_prints_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_row(monkeypatch, task=_task(), worktree=None)
    _requeue_echo._echo_regress_findings(_TICKET, "test")
    assert "could not be determined" in capsys.readouterr().err


def test_read_error_prints_stderr_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        msg = "queue unreadable"
        raise CwError(msg)

    monkeypatch.setattr(_requeue_echo, "load_dev_queue", lambda: None)
    monkeypatch.setattr(_requeue_echo, "_find_ticket", _boom)
    _requeue_echo._echo_regress_findings(_TICKET, "test")
    assert "could not be determined" in capsys.readouterr().err
