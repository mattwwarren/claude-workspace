"""``cw dev-queue show`` and the row echo on the five mutating verbs (#2691)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import click
import cw.cli.dev_queue.show as show_mod
import pytest
from click.testing import CliRunner, Result
from cw.models.ticket_view import TicketView

from cw.cli import main
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import (
    MustFixOverride,
    QueueItemStatus,
    Stage,
)
from tests._ticket_view_helpers import (
    bound_session_for,
    patch_roster,
    seed_queue,
    write_lane_client,
)
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask

_C = "acme"
_T = "ACM-1"
_HEAD = "0123456789abcdef0123456789abcdef01234567"
_SHOW_LOGGER = "cw.cli.dev_queue.show"


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lane_client(_C, tmp_path / "ws", lanes=("default", "fast"))
    patch_roster(monkeypatch, set())


def _task(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"ticket_id": _T, "client": _C}
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["dev-queue", *args])


def _lines(result: Result) -> list[str]:
    return result.stdout.splitlines()


def _ticket_lines(result: Result) -> list[str]:
    return [ln for ln in _lines(result) if ln.startswith("ticket: ")]


def _seed_review_pending(*, signoff: bool = False) -> None:
    task = _task(
        status=QueueItemStatus.BLOCKED_ON_USER,
        stage=Stage.REVIEW,
        session_id="sess-1",
        signoff="operator" if signoff else None,
    )
    seed_queue(
        [task],
        [bound_session_for(task, last_result={"status": "review_pending_approval"})],
    )


class TestShow:
    @pytest.mark.parametrize(
        ("ticket", "status"),
        [
            ("ACM-1", QueueItemStatus.PENDING),
            ("ACM-2", QueueItemStatus.RUNNING),
            ("ACM-3", QueueItemStatus.BLOCKED_ON_USER),
            ("ACM-4", QueueItemStatus.COMPLETED),
        ],
    )
    def test_text(self, ticket: str, status: QueueItemStatus) -> None:
        seed_queue([_task(ticket_id=ticket, status=status)])
        result = _invoke("show", ticket, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0] == f"ticket: {ticket}"
        assert f"status: {status.value}" in lines
        assert "next:" in lines

    def test_json(self) -> None:
        seed_queue([_task()])
        result = _invoke("show", _T, "-c", _C, "--json")
        assert result.exit_code == 0, result.output
        view = TicketView.model_validate_json(result.stdout)
        assert (view.ticket_id, view.client) == (_T, _C)
        assert view.schema_version == 1

    def test_client_auto_found(self) -> None:
        seed_queue([_task()])
        result = _invoke("show", _T)
        assert result.exit_code == 0, result.output
        assert f"client: {_C}" in _lines(result)

    def test_ambiguous_client(self) -> None:
        seed_queue([_task(), _task(client="beta")])
        result = _invoke("show", _T)
        assert result.exit_code == 1
        assert _C in result.output
        assert "beta" in result.output
        assert "-c" in result.output

    def test_unknown_ticket(self) -> None:
        seed_queue([_task()])
        result = _invoke("show", "ACM-404")
        assert result.exit_code == 1
        assert "No dev-queue task found" in result.output

    def test_wrong_client(self) -> None:
        seed_queue([_task()])
        result = _invoke("show", _T, "-c", "wrongclient")
        assert result.exit_code == 1
        assert "No dev-queue task found" in result.output


class TestAddEcho:
    def test_enqueued(self) -> None:
        seed_queue([])
        result = _invoke("add", "ACM-5", "-c", _C)
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0] == f"Enqueued ACM-5 -> {_C} (priority=0)"
        assert lines[1] == ""
        assert lines[2] == "ticket: ACM-5"
        assert "status: pending" in lines

    def test_skipped_echoes_existing_row(self) -> None:
        seed_queue([_task(status=QueueItemStatus.BLOCKED_ON_USER)])
        result = _invoke("add", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        assert result.stderr.startswith(f"Skipped {_T} -> {_C}: already queued")
        assert _ticket_lines(result) == [f"ticket: {_T}"]
        assert "status: blocked_on_user" in _lines(result)

    def test_two_tickets_two_blocks(self) -> None:
        seed_queue([])
        result = _invoke("add", "ACM-5", "ACM-6", "-c", _C)
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        order = [ln for ln in lines if ln.startswith(("Enqueued", "ticket: "))]
        assert order == [
            f"Enqueued ACM-5 -> {_C} (priority=0)",
            "ticket: ACM-5",
            f"Enqueued ACM-6 -> {_C} (priority=0)",
            "ticket: ACM-6",
        ]


class TestMoveEcho:
    def test_lane(self) -> None:
        seed_queue([_task()])
        result = _invoke("move", _T, "-c", _C, "--to", "fast")
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0] == f"Moved {_T} ({_C}): default -> fast"
        assert "lane: fast" in lines

    def test_priority(self) -> None:
        seed_queue([_task()])
        result = _invoke("move", _T, "-c", _C, "--priority", "5")
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0] == f"Reprioritized {_T} ({_C}): 0 -> 5"
        assert "priority: 5" in lines
        assert len(_ticket_lines(result)) == 1


class TestRequeueEcho:
    def test_requeue(self) -> None:
        seed_queue([_task(status=QueueItemStatus.BLOCKED_ON_USER)])
        result = _invoke("requeue", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0] == f"Requeued {_T} ({_C}): plan -> plan (PENDING)"
        assert lines[1] == ""
        assert lines[2] == f"ticket: {_T}"
        assert "status: pending" in lines


class TestApproveEcho:
    def test_direct_advance(self) -> None:
        _seed_review_pending()
        result = _invoke("approve", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0] == f"Approved {_T} ({_C}): review -> finalize"
        assert "stage: finalize" in lines
        assert len(_ticket_lines(result)) == 1

    def test_awaiting_signoff(self) -> None:
        _seed_review_pending(signoff=True)
        result = _invoke("approve", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0] == (
            f"Approved {_T} ({_C}): parked at review awaiting operator signoff"
            " before it ships. Run 'approve' again to clear the gate."
        )
        assert "status: awaiting_operator_signoff" in lines

    def test_plan_requeued_block_follows_post_marker_tip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([_task(status=QueueItemStatus.PENDING)])
        monkeypatch.setattr(
            "cw.cli.dev_queue.approve.approve_ticket",
            lambda *_a, **_kw: {
                "from_stage": "plan",
                "to_stage": "plan",
                "ticket_id": _T,
                "client": _C,
                "awaiting_signoff": False,
                "plan_requeued": True,
                "finalize_held": False,
                "plan_approved_fingerprint": None,
                "plan_promoted": False,
                "body_drift_warning": None,
            },
        )
        result = _invoke("approve", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0].startswith(f"Approved {_T} ({_C}): plan not yet")
        tip = next(
            i for i, ln in enumerate(lines) if ln.startswith("Pass --post-marker")
        )
        assert tip < lines.index(f"ticket: {_T}")

    def test_scope_drift_shows_grant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue(
            [
                _task(
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    stage=Stage.IMPL,
                    blocked_reason="plan_scope_drift",
                )
            ]
        )
        monkeypatch.setattr(
            "cw.dev_queue.approval.branch_head_sha_on_origin",
            lambda *_a, **_kw: (_HEAD, True),
        )
        result = _invoke("approve", _T, "-c", _C, "--scope-drift", "b.py,a.py")
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0].startswith(
            f"Approved scope drift for {_T} ({_C}): impl -> impl."
        )
        assert "approval: scope-drift grant: files=a.py,b.py head=0123456789ab" in lines

    def test_override_must_fix_shows_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue(
            [
                _task(
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    stage=Stage.REVIEW,
                    blocked_reason=CODEX_MUST_FIX_FINDINGS,
                )
            ]
        )

        def _fake_override(
            ticket_id: str, client: str, reason: str, **_kw: object
        ) -> dict[str, object]:
            store = load_dev_queue()
            store.tasks[0].must_fix_override = MustFixOverride(
                actor="alice",
                reason=reason,
                reviewed_sha="a" * 40,
                finding_ids=[("f", "x")],
                recorded_at=datetime(2026, 10, 15, 17, 45, tzinfo=UTC),
            )
            save_dev_queue(store)
            return {
                "ticket_id": ticket_id,
                "client": client,
                "stage": "review",
                "actor": "alice",
                "reviewed_sha": "a" * 40,
                "finding_ids": [("f", "x")],
            }

        monkeypatch.setattr(
            "cw.cli.dev_queue.approve.approve_must_fix_override_ticket",
            _fake_override,
        )
        result = _invoke(
            "approve", _T, "-c", _C, "--override-must-fix", "--reason", "fp"
        )
        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert lines[0].startswith(f"Recorded MUST_FIX override for {_T} ({_C})")
        assert (
            "approval: must-fix override by alice at 2026-10-15T17:45:00Z: fp" in lines
        )

    def test_row_echoed_before_orphan_close_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_review_pending()

        def _close_fails(*_a: object, **_kw: object) -> None:
            msg = "close failed"
            raise CwError(msg)

        monkeypatch.setattr(
            "cw.cli.dev_queue.approve._close_orphans_after_approve", _close_fails
        )
        result = _invoke("approve", _T, "-c", _C)
        assert result.exit_code == 1
        assert f"ticket: {_T}" in _lines(result)
        assert "close failed" in result.stderr


class TestCancelEcho:
    def test_two_tickets(self) -> None:
        seed_queue([_task(), _task(ticket_id="ACM-2")])
        result = _invoke("cancel", _T, "ACM-2", "-c", _C)
        assert result.exit_code == 0, result.output
        order = [
            ln
            for ln in _lines(result)
            if ln.startswith(("Cancelled", "ticket: ", "status: "))
        ]
        assert order == [
            f"Cancelled {_T} in {_C} dev-queue.",
            f"ticket: {_T}",
            "status: cancelled",
            f"Cancelled ACM-2 in {_C} dev-queue.",
            "ticket: ACM-2",
            "status: cancelled",
        ]


class TestEchoIsBestEffort:
    def test_no_duplicate_row_warning(self) -> None:
        seed_queue([_task(), _task()])
        result = _invoke("add", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        assert "Warning" not in result.stderr
        assert _ticket_lines(result) == [f"ticket: {_T}"]
        assert "other_rows: 1" in _lines(result)

    def test_add_without_persisted_row(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        seed_queue([])
        monkeypatch.setattr("cw.cli.dev_queue.crud.add_ticket", lambda *_a, **_kw: True)
        with caplog.at_level(logging.DEBUG, logger=_SHOW_LOGGER):
            result = _invoke("add", "ACM-9", "-c", _C)
        assert result.exit_code == 0, result.output
        assert result.stdout == f"Enqueued ACM-9 -> {_C} (priority=0)\n"
        swallowed = [r for r in caplog.records if r.name == _SHOW_LOGGER]
        assert swallowed
        assert swallowed[0].levelno == logging.DEBUG
        assert swallowed[0].exc_info is not None

    def test_approve_without_persisted_row(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        seed_queue([])
        monkeypatch.setattr(
            "cw.cli.dev_queue.approve.approve_ticket",
            lambda *_a, **_kw: {
                "from_stage": "review",
                "to_stage": "finalize",
                "ticket_id": _T,
                "client": _C,
                "awaiting_signoff": False,
                "plan_requeued": False,
                "finalize_held": False,
                "plan_approved_fingerprint": None,
                "plan_promoted": False,
                "body_drift_warning": None,
            },
        )
        with caplog.at_level(logging.DEBUG, logger=_SHOW_LOGGER):
            result = _invoke("approve", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        assert result.stdout == f"Approved {_T} ({_C}): review -> finalize\n"
        assert [r for r in caplog.records if r.name == _SHOW_LOGGER]


def _event_count() -> int:
    return len(read_events())


def _counting_echo(
    monkeypatch: pytest.MonkeyPatch, target: str, deltas: list[int]
) -> None:
    """Wrap the real echo at *target*, recording the events each call adds."""
    real: Callable[..., None] = show_mod.echo_resulting_row

    def _wrapped(*args: str, **kwargs: object) -> None:
        before = _event_count()
        real(*args, **kwargs)
        deltas.append(_event_count() - before)

    monkeypatch.setattr(target, _wrapped)


class TestNoNewEmissions:
    def test_show_records_nothing(self) -> None:
        seed_queue([_task()])
        before = _event_count()
        assert _invoke("show", _T, "-c", _C).exit_code == 0
        assert _invoke("show", _T, "-c", _C, "--json").exit_code == 0
        assert _event_count() == before

    def test_cancel_echo_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        deltas: list[int] = []
        _counting_echo(monkeypatch, "cw.cli.dev_queue.crud.echo_resulting_row", deltas)
        seed_queue([_task(), _task(ticket_id="ACM-2")])
        assert _invoke("cancel", _T, "ACM-2", "-c", _C).exit_code == 0
        assert deltas == [0, 0]

    def test_approve_echo_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deltas: list[int] = []
        _counting_echo(
            monkeypatch, "cw.cli.dev_queue.approve.echo_resulting_row", deltas
        )
        _seed_review_pending()
        assert _invoke("approve", _T, "-c", _C).exit_code == 0
        assert deltas == [0]


class TestRegistration:
    def test_registered(self) -> None:
        ctx = click.Context(main)
        group = main.get_command(ctx, "dev-queue")
        assert isinstance(group, click.Group)
        assert group.get_command(ctx, "show") is not None

    def test_help_names_options(self) -> None:
        result = _invoke("show", "--help")
        assert result.exit_code == 0
        assert "-c, --client" in result.output
        assert "--json" in result.output
