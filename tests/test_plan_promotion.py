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
    from collections.abc import Callable

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

    promoted = promote_plan_draft(
        _plan_task(cw_dir.parent),
        sample_client,
    )

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
        promote_plan_draft(
            _plan_task(cw_dir.parent),
            sample_client,
        )

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
        promote_plan_draft(
            _plan_task(cw_dir.parent),
            sample_client,
        )

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
        promote_plan_draft(
            task,
            sample_client,
        )

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

    promoted = promote_plan_draft(
        _plan_task(cw_dir.parent),
        sample_client,
    )

    assert promoted is True
    assert (cw_dir / "plan.md").read_text(encoding="utf-8") == _DRAFT_BODY
    assert (cw_dir / "plan-draft.md").exists()


def test_promote_plan_draft_accepts_legacy_approval_without_fingerprint(
    tmp_path: Path, sample_client: ClientConfig
) -> None:
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan-draft.md").write_text(_DRAFT_BODY, encoding="utf-8")

    assert promote_plan_draft(_plan_task(cw_dir.parent), sample_client) is True

    assert (cw_dir / "plan.md").read_text(encoding="utf-8") == _DRAFT_BODY


def test_draft_fingerprint_only_strips_leading_bookkeeping_lines() -> None:
    from cw.dev_queue.plan_promotion import _draft_fingerprint

    leading = "<!-- plan-stage-scan-round: 1 -->\n"
    body = "<!-- plan-stage-settled: A1: ADOPTED -->\n\nbody\n"
    interior = "body\n<!-- plan-stage-settled: A1: ADOPTED -->\n"

    assert _draft_fingerprint(leading + body) != _draft_fingerprint(leading + interior)


def _record_events(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    recorded: list[object] = []

    def _recording_event(*args: object, **_kwargs: object) -> None:
        recorded.append(args[0])

    monkeypatch.setattr("cw.dev_queue.plan_promotion.record_event", _recording_event)
    return recorded


def _clobbering_write(real_calls_after: int | None) -> Callable[[Path, str], None]:
    """A write that leaves partial content then raises, until N calls in."""
    calls: list[Path] = []

    def _write(path: Path, text: str) -> None:
        calls.append(path)
        if real_calls_after is not None and len(calls) > real_calls_after:
            path.write_text(text, encoding="utf-8")
            return
        path.write_text("partial", encoding="utf-8")
        msg = "simulated disk full"
        raise OSError(msg)

    return _write


def test_promote_plan_draft_write_failure_restores_prior_plan(
    tmp_path: Path, sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan.md").write_text(_STALE_PLAN_BODY, encoding="utf-8")
    (cw_dir / "plan-draft.md").write_text(_DRAFT_BODY, encoding="utf-8")
    recorded = _record_events(monkeypatch)
    monkeypatch.setattr(
        "cw.dev_queue.plan_promotion.atomic_write_text",
        _clobbering_write(real_calls_after=1),
    )

    with pytest.raises(ApproveGateError) as excinfo:
        promote_plan_draft(
            _plan_task(cw_dir.parent),
            sample_client,
        )

    message = str(excinfo.value)
    assert str(cw_dir / "plan.md") in message
    assert str(cw_dir / "plan-draft.md") in message
    assert "OSError: simulated disk full" in message
    assert "was restored" in message
    assert "RESTORE FAILED" not in message
    assert (cw_dir / "plan.md").read_text(encoding="utf-8") == _STALE_PLAN_BODY
    assert (cw_dir / "plan-draft.md").read_text(encoding="utf-8") == _DRAFT_BODY
    assert recorded == []


def test_promote_plan_draft_write_failure_with_failed_restore_names_manual_step(
    tmp_path: Path, sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan.md").write_text(_STALE_PLAN_BODY, encoding="utf-8")
    (cw_dir / "plan-draft.md").write_text(_DRAFT_BODY, encoding="utf-8")
    recorded = _record_events(monkeypatch)
    monkeypatch.setattr(
        "cw.dev_queue.plan_promotion.atomic_write_text",
        _clobbering_write(real_calls_after=None),
    )

    with pytest.raises(ApproveGateError) as excinfo:
        promote_plan_draft(
            _plan_task(cw_dir.parent),
            sample_client,
        )

    message = str(excinfo.value)
    assert str(cw_dir.parent) in message
    assert "RESTORE FAILED" in message
    assert f"the approved draft is still intact at {cw_dir / 'plan-draft.md'}" in (
        message
    )
    assert "re-run `cw dev-queue approve GEN-2342 --client test-client`" in message
    assert isinstance(excinfo.value.__cause__, OSError)
    assert (cw_dir / "plan-draft.md").read_text(encoding="utf-8") == _DRAFT_BODY
    assert recorded == []


def test_promote_plan_draft_audit_event_failure_is_non_fatal(
    tmp_path: Path, sample_client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promotion audit event is best-effort telemetry after success."""
    cw_dir = _cw_dir(tmp_path)
    (cw_dir / "plan-draft.md").write_text(_DRAFT_BODY, encoding="utf-8")

    def _failing_event(*_args: object, **_kwargs: object) -> None:
        msg = "event inbox unavailable"
        raise OSError(msg)

    monkeypatch.setattr("cw.dev_queue.plan_promotion.record_event", _failing_event)

    promoted = promote_plan_draft(
        _plan_task(cw_dir.parent),
        sample_client,
    )

    assert promoted is True
    assert (cw_dir / "plan.md").read_text(encoding="utf-8") == _DRAFT_BODY
    assert not (cw_dir / "plan-draft.md").exists()
