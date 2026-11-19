"""Spawn-time render seam: store-only re-render and failure event (RFC 0019 T1)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dispatch.gating import context_json
from cw.dispatch.gating.context_json import _render_context_json_from_snapshot
from cw.models import OrchestratorEventType
from cw.ticket_snapshot import snapshot_path
from tests._shared.ticket_context import make_snapshot, seeded_row
from tests._ticket_view_helpers import ACME, ACME_TICKET, events_of, make_task

if TYPE_CHECKING:
    from cw.models import TicketTask


def _seeded_task(tmp_path: Path) -> tuple[TicketTask, Path]:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    task = seeded_row(make_snapshot(ticket_id=ACME_TICKET), seed=False)
    return task, worktree


def test_no_digest_is_a_noop(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _render_context_json_from_snapshot(make_task(), worktree)
    assert not (worktree / ".cw" / "context.json").exists()
    assert events_of(OrchestratorEventType.TICKET_SNAPSHOT_RENDER_FAILED) == []


def test_store_render_writes_context_json(tmp_path: Path) -> None:
    task, worktree = _seeded_task(tmp_path)
    _render_context_json_from_snapshot(task, worktree)
    rendered = json.loads(
        (worktree / ".cw" / "context.json").read_text(encoding="utf-8")
    )
    assert rendered["ticket_id"] == ACME_TICKET
    assert rendered["materialized_by_session"] is None
    assert events_of(OrchestratorEventType.TICKET_SNAPSHOT_RENDER_FAILED) == []


def test_missing_store_file_records_cause_missing(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    task = make_task(snapshot_digest="c" * 64)
    _render_context_json_from_snapshot(task, worktree)
    assert not (worktree / ".cw" / "context.json").exists()
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_RENDER_FAILED)
    assert set(event.payload) == {"ticket_id", "client", "snapshot_digest", "cause"}
    assert event.payload["cause"] == "missing"
    assert event.payload["ticket_id"] == ACME_TICKET
    assert event.payload["client"] == ACME
    assert event.correlation_id == ACME_TICKET
    assert event.payload["snapshot_digest"] == "c" * 64


def test_mismatched_store_content_records_cause_mismatch(tmp_path: Path) -> None:
    task, worktree = _seeded_task(tmp_path)
    path = snapshot_path(ACME, task.snapshot_digest or "")
    path.write_text(path.read_text(encoding="ascii") + " ", encoding="ascii")
    _render_context_json_from_snapshot(task, worktree)
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_RENDER_FAILED)
    assert event.payload["cause"] == "mismatch"


def test_render_oserror_records_cause_render_error_and_spawns(tmp_path: Path) -> None:
    task, worktree = _seeded_task(tmp_path)
    (worktree / ".cw").write_text("a regular file, not a dir", encoding="utf-8")
    _render_context_json_from_snapshot(task, worktree)
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_RENDER_FAILED)
    assert event.payload["cause"] == "render_error"


def test_event_write_failure_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        msg = "disk gone"
        raise OSError(msg)

    monkeypatch.setattr(context_json, "record_event", _boom)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        _render_context_json_from_snapshot(
            make_task(snapshot_digest="d" * 64), worktree
        )
    assert "not recorded" in caplog.text


def test_failure_log_carries_no_ticket_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    task, worktree = _seeded_task(tmp_path)
    path = snapshot_path(ACME, task.snapshot_digest or "")
    path.write_text(path.read_text(encoding="ascii") + " ", encoding="ascii")
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        _render_context_json_from_snapshot(task, worktree)
    assert "Authored test ticket body" not in caplog.text
    assert "cause=mismatch" in caplog.text


def test_failed_render_removes_stale_context_json(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    (worktree / ".cw").mkdir(parents=True)
    stale = worktree / ".cw" / "context.json"
    stale.write_text("{}", encoding="utf-8")
    _render_context_json_from_snapshot(make_task(snapshot_digest="c" * 64), worktree)
    assert not stale.exists()
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_RENDER_FAILED)
    assert event.payload["cause"] == "missing"


def test_render_failure_with_unremovable_context_blocks_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stale = tmp_path / "wt" / ".cw" / "context.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"sentinel": "stale"}', encoding="utf-8")
    original_replace = Path.replace
    original_unlink = Path.unlink

    def _reject_replace(self: Path, target: Path) -> None:
        if self == stale:
            msg = "rename denied"
            raise OSError(msg)
        original_replace(self, target)

    def _reject_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self == stale:
            msg = "unlink denied"
            raise OSError(msg)
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "replace", _reject_replace)
    monkeypatch.setattr(Path, "unlink", _reject_unlink)
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        can_spawn = _render_context_json_from_snapshot(
            make_task(snapshot_digest="c" * 64), tmp_path / "wt"
        )
    assert not can_spawn
    assert stale.exists()
    assert "could not remove stale .cw/context.json" in caplog.text
    (event,) = events_of(OrchestratorEventType.TICKET_SNAPSHOT_RENDER_FAILED)
    assert event.payload["cause"] == "missing"
