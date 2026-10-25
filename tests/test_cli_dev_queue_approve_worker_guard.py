"""CLI end-to-end: ``cw dev-queue approve`` refuses inside a dispatch worker (#2776).

``approve`` is the single writer of the approval evidence a downstream plan or
ship gate trusts (plan approval, REVIEW-gate release, scope-drift grant,
MUST_FIX override), so a headless worker must not mint its own. Every case
sets its own session context in ``tmp_path`` because the suite may itself run
inside a headless dispatch worker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.config import dev_queue_file
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    PLAN_PENDING,
    REVIEW_PENDING,
    bound_session_for,
    install_acme_client,
    invoke_dev_queue,
    make_blocked,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

_APPROVE_MODES: dict[str, tuple[str, ...]] = {
    "plain": (),
    "post-marker": ("--post-marker",),
    "scope-drift": ("--scope-drift", "a.py,b.py"),
    "override-must-fix": ("--override-must-fix", "--reason", "x"),
}


@pytest.fixture(autouse=True)
def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)


def _no_gh(*_a: object, **_k: object) -> None:
    msg = "gh must not be called from a refused approve"
    raise AssertionError(msg)


def _seed_parked(last_result: dict[str, object], stage: Stage) -> None:
    task = make_blocked(stage=stage)
    seed_queue([task], [bound_session_for(task, last_result=last_result)])


def _approved_events() -> list[object]:
    return list(read_events(event_types=[OrchestratorEventType.TICKET_APPROVED]))


def _approve(*extra: str) -> Result:
    return invoke_dev_queue("approve", ACME_TICKET, "-c", ACME, *extra)


class TestApproveWorkerRefusal:
    @pytest.fixture(autouse=True)
    def _no_tracker_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.cli.dev_queue.approve.post_issue_comment", _no_gh)
        monkeypatch.setattr("cw.cli.dev_queue.approve.fetch_issue_comments", _no_gh)

    def _assert_refused(self, result: Result, queue_before: bytes) -> None:
        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert "orchestrator session" in result.output
        assert "cw dev-queue approve" in result.output
        assert dev_queue_file().read_bytes() == queue_before
        assert _approved_events() == []

    @pytest.mark.parametrize("mode", sorted(_APPROVE_MODES))
    @pytest.mark.parametrize(
        ("last_result", "stage"),
        [(PLAN_PENDING, Stage.PLAN), (REVIEW_PENDING, Stage.REVIEW)],
        ids=["plan-gate", "review-gate"],
    )
    def test_headless_context_refuses_every_mode(
        self,
        tmp_path: Path,
        mode: str,
        last_result: dict[str, object],
        stage: Stage,
    ) -> None:
        _seed_parked(last_result, stage)
        _write_session_context(tmp_path, headless=True)
        before = dev_queue_file().read_bytes()
        self._assert_refused(_approve(*_APPROVE_MODES[mode]), before)

    def test_worker_tmpdir_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_parked(PLAN_PENDING, Stage.PLAN)
        worker = tmp_path / "worker-wt"
        _write_session_context(worker, headless=True)
        scratch = worker / ".cw" / "tmp"
        scratch.mkdir(parents=True)
        monkeypatch.setenv("TMPDIR", str(scratch))
        before = dev_queue_file().read_bytes()
        self._assert_refused(_approve(), before)

    def test_context_lookup_happens_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_parked(PLAN_PENDING, Stage.PLAN)
        monkeypatch.setattr(
            "cw.cli._hook_io.find_cw_context", lambda _path: {"headless": True}
        )
        before = dev_queue_file().read_bytes()
        self._assert_refused(_approve(), before)

    def test_guard_precedes_usage_validation(self, tmp_path: Path) -> None:
        _seed_parked(PLAN_PENDING, Stage.PLAN)
        _write_session_context(tmp_path, headless=True)
        before = dev_queue_file().read_bytes()
        result = _approve("--scope-drift", "x", "--post-marker")
        self._assert_refused(result, before)
        assert "mutually exclusive" not in result.output


class TestUnguardedPaths:
    def test_unguarded_outside_a_worker(self) -> None:
        _seed_parked(REVIEW_PENDING, Stage.REVIEW)
        result = _approve()
        assert result.exit_code == 0, result.output
        assert "Approved" in result.output
        assert len(_approved_events()) == 1

    def test_revoke_plan_approval_still_runs_in_worker(self, tmp_path: Path) -> None:
        # The headless plan-stage worker legitimately runs this verb.
        task = make_blocked(stage=Stage.PLAN)
        seed_queue([task], [bound_session_for(task, last_result=PLAN_PENDING)])
        _write_session_context(tmp_path, headless=True)
        result = invoke_dev_queue("revoke-plan-approval", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 0, result.output
        row = next(t for t in load_dev_queue().tasks if t.ticket_id == ACME_TICKET)
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
