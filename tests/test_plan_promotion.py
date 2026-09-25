"""Tests for cw.dev_queue.plan_promotion — promote plan-draft.md on approve (#2342)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue.plan_promotion import promote_plan_draft
from cw.exceptions import ApproveGateError
from cw.models import QueueItemStatus, Stage
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask

_DRAFT_BODY = "# Plan\n\nreconciled draft body\n"
_STALE_PLAN_BODY = "# Plan\n\nstale pre-reconciliation body\n"


def _plan_task(worktree: Path | None) -> TicketTask:
    return _make_ticket_task(
        ticket_id="GEN-2342",
        client="test-client",
        stage=Stage.PLAN,
        status=QueueItemStatus.BLOCKED_ON_USER,
        worktree_path=worktree,
    )


def _cw_dir(tmp_path: Path) -> Path:
    cw_dir = tmp_path / "wt" / ".cw"
    cw_dir.mkdir(parents=True)
    return cw_dir


def test_promote_plan_draft_writes_plan_md_and_removes_draft(
    tmp_path: Path, sample_client: ClientConfig
) -> None:
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan-draft.md").write_text(_DRAFT_BODY, encoding="utf-8")
    (cw_dir / "plan.md").write_text(_STALE_PLAN_BODY, encoding="utf-8")

    promoted = promote_plan_draft(_plan_task(cw_dir.parent), sample_client)

    assert promoted is True
    assert (cw_dir / "plan.md").read_text(encoding="utf-8") == _DRAFT_BODY
    assert not (cw_dir / "plan-draft.md").exists()


def test_promote_plan_draft_no_draft_present_returns_false(
    tmp_path: Path, sample_client: ClientConfig
) -> None:
    cw_dir = _cw_dir(tmp_path)

    promoted = promote_plan_draft(_plan_task(cw_dir.parent), sample_client)

    assert promoted is False
    assert not (cw_dir / "plan.md").exists()


def test_promote_plan_draft_no_worktree_returns_false(
    sample_client: ClientConfig,
) -> None:
    promoted = promote_plan_draft(_plan_task(None), sample_client)

    assert promoted is False


def test_promote_plan_draft_read_failure_raises_approve_gate_error(
    tmp_path: Path, sample_client: ClientConfig
) -> None:
    """A draft that exists but cannot be read (here: a directory, which raises
    IsADirectoryError, an OSError subclass) fails loud before any write."""
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan-draft.md").mkdir()

    with pytest.raises(ApproveGateError):
        promote_plan_draft(_plan_task(cw_dir.parent), sample_client)

    assert not (cw_dir / "plan.md").exists()


def test_promote_plan_draft_write_failure_raises_and_leaves_draft_intact(
    tmp_path: Path, sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan-draft.md").write_text(_DRAFT_BODY, encoding="utf-8")

    def _failing_write(_path: Path, _text: str) -> None:
        msg = "simulated disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.dev_queue.plan_promotion.atomic_write_text", _failing_write)

    with pytest.raises(ApproveGateError):
        promote_plan_draft(_plan_task(cw_dir.parent), sample_client)

    assert (cw_dir / "plan-draft.md").read_text(encoding="utf-8") == _DRAFT_BODY
    assert not (cw_dir / "plan.md").exists()


def test_promote_plan_draft_error_names_worktree_path_and_exception(
    tmp_path: Path, sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan-draft.md").write_text(_DRAFT_BODY, encoding="utf-8")
    task = _plan_task(cw_dir.parent)

    def _failing_write(_path: Path, _text: str) -> None:
        msg = "simulated disk full"
        raise PermissionError(msg)

    monkeypatch.setattr("cw.dev_queue.plan_promotion.atomic_write_text", _failing_write)

    with pytest.raises(ApproveGateError) as excinfo:
        promote_plan_draft(task, sample_client)

    message = str(excinfo.value)
    assert str(task.worktree_path) in message
    assert "PermissionError" in message
    assert "simulated disk full" in message
    assert isinstance(excinfo.value.__cause__, PermissionError)


def test_promote_plan_draft_draft_delete_failure_is_non_fatal(
    tmp_path: Path, sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the read/write half is fail-loud; clearing the draft afterwards is
    best-effort, mirroring auto-dev-plan.md Step 1g."""
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan-draft.md").write_text(_DRAFT_BODY, encoding="utf-8")

    def _failing_unlink(_self: Path, *, missing_ok: bool = False) -> None:
        msg = "simulated unlink failure"
        raise OSError(msg)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    promoted = promote_plan_draft(_plan_task(cw_dir.parent), sample_client)

    assert promoted is True
    assert (cw_dir / "plan.md").read_text(encoding="utf-8") == _DRAFT_BODY
    assert (cw_dir / "plan-draft.md").exists()
