"""``cw dev-queue show`` and the row echo on the five mutating verbs (#2691)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import click
import pytest
from click.testing import Result
from freezegun import freeze_time

import cw.cli.dev_queue.show as show_mod
from cw.cli import main
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.dev_queue import (
    FINALIZE_GATE_HELD_DISPOSITION,
    load_dev_queue,
    save_dev_queue,
)
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_LANE,
    LaneConcurrencyOverride,
    MustFixOverride,
    QueueItemStatus,
    Stage,
)
from cw.models.ticket_view import NextCommand, TicketView
from tests._cli_review_helpers import _enter_operator_machine
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    REVIEW_PENDING,
    bound_session_for,
    install_acme_client,
    invoke_dev_queue,
    make_task,
    seed_queue,
    stdout_lines,
    write_lane_client,
    write_lane_override,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask

_STAGE_COMPLETE: dict[str, object] = {"status": "stage_complete"}


_C = ACME
_T = ACME_TICKET
_HEAD = "0123456789abcdef0123456789abcdef01234567"
_SHOW_LOGGER = "cw.cli.dev_queue.show"


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)


def _ticket_lines(result: Result) -> list[str]:
    return [ln for ln in stdout_lines(result) if ln.startswith("ticket: ")]


def _seed_review_pending(*, signoff: bool = False) -> None:
    task = make_task(
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
        seed_queue([make_task(ticket_id=ticket, status=status)])
        result = invoke_dev_queue("show", ticket, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0] == f"ticket: {ticket}"
        assert f"status: {status.value}" in lines
        assert "next:" in lines

    def test_json(self) -> None:
        seed_queue([make_task()])
        result = invoke_dev_queue("show", _T, "-c", _C, "--json")
        assert result.exit_code == 0, result.output
        view = TicketView.model_validate_json(result.stdout)
        assert (view.ticket_id, view.client) == (_T, _C)
        assert view.schema_version == 1

    def test_client_auto_found(self) -> None:
        seed_queue([make_task()])
        result = invoke_dev_queue("show", _T)
        assert result.exit_code == 0, result.output
        assert f"client: {_C}" in stdout_lines(result)

    def test_ambiguous_client(self) -> None:
        seed_queue([make_task(), make_task(client="beta")])
        result = invoke_dev_queue("show", _T)
        assert result.exit_code == 1
        assert _C in result.output
        assert "beta" in result.output
        assert "-c" in result.output

    def test_unknown_ticket(self) -> None:
        seed_queue([make_task()])
        result = invoke_dev_queue("show", "ACM-404")
        assert result.exit_code == 1
        assert "No dev-queue task found" in result.output

    def test_wrong_client(self) -> None:
        seed_queue([make_task()])
        result = invoke_dev_queue("show", _T, "-c", "wrongclient")
        assert result.exit_code == 1
        assert "No dev-queue task found" in result.output


_PAUSED_AT = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _seed_breaker_pause() -> None:
    write_lane_override(
        _C,
        "default",
        override=LaneConcurrencyOverride(
            paused=True,
            paused_at=_PAUSED_AT,
            pause_source="circuit_breaker",
            pause_trigger="spawn_errors",
            consecutive_spawn_errors=3,
            recent_spawn_errors=["boom"],
            circuit_trip_streak=1,
            circuit_auto_resume_at=_PAUSED_AT + timedelta(minutes=15),
        ),
    )
    seed_queue([make_task()])


class TestShowLanePause:
    """#2696: ``show`` names the pause cause and the resume command."""

    def test_text(self) -> None:
        _seed_breaker_pause()

        with freeze_time(_PAUSED_AT + timedelta(minutes=5)):
            result = invoke_dev_queue("show", _T, "-c", _C)

        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        start = lines.index(
            "skip_reason: lane_circuit_paused"
            " (source=row, lane paused by its circuit breaker)"
        )
        assert lines[start + 1 : start + 6] == [
            "  paused since 2026-10-18T12:00:00Z (5m ago) by circuit_breaker"
            " (spawn errors, trip #1)",
            "    last spawn errors (oldest first):",
            "      - boom",
            "    auto-resume: 2026-10-18T12:15:00Z (in 10m)",
            "    resume: cw lane resume acme default",
        ]

    def test_json(self) -> None:
        _seed_breaker_pause()

        result = invoke_dev_queue("show", _T, "-c", _C, "--json")

        assert result.exit_code == 0, result.output
        view = TicketView.model_validate_json(result.stdout)
        assert view.dispatch is not None
        pause = view.dispatch.lane_pause
        assert pause is not None
        assert pause.source == "circuit_breaker"
        assert pause.since == _PAUSED_AT
        assert pause.recent_errors == ["boom"]
        assert pause.resume_command == "cw lane resume acme default"


class TestAddEcho:
    def test_enqueued(self) -> None:
        seed_queue([])
        result = invoke_dev_queue("add", "ACM-5", "-c", _C)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0] == f"Enqueued ACM-5 -> {_C} (priority=0)"
        assert lines[1] == ""
        assert lines[2] == "ticket: ACM-5"
        assert "status: pending" in lines

    def test_skipped_echoes_existing_row(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.BLOCKED_ON_USER)])
        result = invoke_dev_queue("add", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        assert result.stderr.startswith(f"Skipped {_T} -> {_C}: already queued")
        assert _ticket_lines(result) == [f"ticket: {_T}"]
        assert "status: blocked_on_user" in stdout_lines(result)

    def test_two_tickets_two_blocks(self) -> None:
        seed_queue([])
        result = invoke_dev_queue("add", "ACM-5", "ACM-6", "-c", _C)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        order = [ln for ln in lines if ln.startswith(("Enqueued", "ticket: "))]
        assert order == [
            f"Enqueued ACM-5 -> {_C} (priority=0)",
            "ticket: ACM-5",
            f"Enqueued ACM-6 -> {_C} (priority=0)",
            "ticket: ACM-6",
        ]


class TestMoveEcho:
    def test_lane(self) -> None:
        seed_queue([make_task()])
        result = invoke_dev_queue("move", _T, "-c", _C, "--to", "fast")
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0] == f"Moved {_T} ({_C}): default -> fast"
        assert "lane: fast" in lines

    def test_priority(self) -> None:
        seed_queue([make_task()])
        result = invoke_dev_queue("move", _T, "-c", _C, "--priority", "5")
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0] == f"Reprioritized {_T} ({_C}): 0 -> 5"
        assert "priority: 5" in lines
        assert len(_ticket_lines(result)) == 1


class TestRequeueEcho:
    def test_requeue(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.BLOCKED_ON_USER)])
        result = invoke_dev_queue("requeue", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0] == f"Requeued {_T} ({_C}): plan -> plan (PENDING)"
        assert lines[1] == ""
        assert lines[2] == f"ticket: {_T}"
        assert "status: pending" in lines


class TestApproveEcho:
    def test_direct_advance(self) -> None:
        _seed_review_pending()
        result = invoke_dev_queue("approve", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0] == f"Approved {_T} ({_C}): review -> finalize"
        assert "stage: finalize" in lines
        assert len(_ticket_lines(result)) == 1

    def test_awaiting_signoff(self) -> None:
        _seed_review_pending(signoff=True)
        result = invoke_dev_queue("approve", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0] == (
            f"Approved {_T} ({_C}): parked at review awaiting operator signoff"
            " before it ships. Run 'approve' again to clear the gate."
        )
        assert "status: awaiting_operator_signoff" in lines

    def test_plan_requeued_block_follows_post_marker_tip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
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
        result = invoke_dev_queue("approve", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0].startswith(f"Approved {_T} ({_C}): plan not yet")
        tip = next(
            i for i, ln in enumerate(lines) if ln.startswith("Pass --post-marker")
        )
        assert tip < lines.index(f"ticket: {_T}")

    def test_scope_drift_shows_grant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue(
            [
                make_task(
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    stage=Stage.IMPL,
                    blocked_reason="plan_scope_drift",
                )
            ]
        )
        monkeypatch.setattr(
            "cw.dev_queue.scope_drift.branch_head_sha_on_origin",
            lambda *_a, **_kw: (_HEAD, True),
        )
        result = invoke_dev_queue("approve", _T, "-c", _C, "--scope-drift", "b.py,a.py")
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0].startswith(
            f"Approved scope drift for {_T} ({_C}): impl -> impl."
        )
        assert "approval: scope-drift grant: files=a.py,b.py head=0123456789ab" in lines

    def test_override_must_fix_shows_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue(
            [
                make_task(
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
        result = invoke_dev_queue(
            "approve", _T, "-c", _C, "--override-must-fix", "--reason", "fp"
        )
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        assert lines[0].startswith(f"Recorded MUST_FIX override for {_T} ({_C})")
        assert lines[0].endswith(
            " If the ticket or lane has a finalize hold, operator signoff or"
            " scope_hint: large, this step is the human's."
        )
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
        result = invoke_dev_queue("approve", _T, "-c", _C)
        assert result.exit_code == 1
        assert f"ticket: {_T}" in stdout_lines(result)
        assert "close failed" in result.stderr


class TestHumanGateShow:
    def test_human_gate_row_lists_only_the_labelled_approve(self) -> None:
        task = make_task(
            status=QueueItemStatus.BLOCKED_ON_USER,
            stage=Stage.REVIEW,
            session_id="sess-1",
            disposition=FINALIZE_GATE_HELD_DISPOSITION,
        )
        seed_queue([task], [bound_session_for(task)])
        result = invoke_dev_queue("show", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        start = lines.index("next:")
        assert lines[start + 1 :] == [
            "  cw dev-queue approve ACM-1 -c acme   # operator-only human gate;"
            " not for an agent to run: release the operator-set finalize hold so"
            " the ticket finalizes"
        ]
        assert not [ln for ln in lines if "requeue" in ln]


_LABEL = "operator-only human gate; not for an agent to run: "
_NO_GATE = (
    "approve cannot take this row because no bound session is at the approval gate"
)
_TERMINAL_REFUSES = "approve refuses at the terminal stage"
_REQUEUE_FINALIZE = f"cw dev-queue requeue {_T} -c {_C} --stage finalize"
_REQUEUE_REVIEW = f"cw dev-queue requeue {_T} -c {_C} --stage review"
_SHOW_CMD = f"cw dev-queue show {_T} -c {_C}"
_PLAIN_REQUEUE = f"cw dev-queue requeue {_T} -c {_C}"
_OFF_PURPOSE = (
    "stage {stage} is not in this row's lane pipeline, so approve and requeue"
    " --stage refuse it until the operator adds it to pipeline.stages or removes"
    " and re-adds the ticket; inspect the row"
)


def _blocked(stage: Stage, **overrides: object) -> TicketTask:
    return make_task(
        status=QueueItemStatus.BLOCKED_ON_USER,
        stage=stage,
        session_id="sess-1",
        **overrides,
    )


def _next_lines() -> list[str]:
    result = invoke_dev_queue("show", _T, "-c", _C)
    assert result.exit_code == 0, result.output
    lines = stdout_lines(result)
    return lines[lines.index("next:") + 1 :]


def _next_json() -> list[dict[str, object]]:
    result = invoke_dev_queue("show", _T, "-c", _C, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    entries = payload["next"]
    assert isinstance(entries, list)
    return entries


def _line(command: str, purpose: str) -> str:
    return f"  {command}   # {purpose}"


class TestTerminalStageShow:
    """#2760: ``show`` never offers an ``approve`` the verb refuses."""

    @pytest.mark.parametrize("lane_override", [False, True])
    def test_rule9_terminal_lists_only_the_requeue(
        self, tmp_path: Path, lane_override: bool
    ) -> None:
        if lane_override:
            write_lane_client(
                _C,
                tmp_path / "ws",
                lane_stages={DEFAULT_LANE: ("plan", "impl", "review", "finalize")},
                client_stages=("plan", "impl"),
            )
        task = _blocked(Stage.FINALIZE, disposition="review_pending_approval")
        seed_queue([task], [bound_session_for(task, last_result=REVIEW_PENDING)])
        assert _next_lines() == [
            _line(
                _REQUEUE_FINALIZE,
                f"re-run the finalize stage; {_TERMINAL_REFUSES}",
            )
        ]
        entries = _next_json()
        assert entries[0]["operator_only"] is False

    def test_human_terminal_at_the_gate(self) -> None:
        task = _blocked(
            Stage.FINALIZE, scope_hint="large", disposition="review_pending_approval"
        )
        seed_queue([task], [bound_session_for(task, last_result=REVIEW_PENDING)])
        purpose = (
            f"{_LABEL}release the finalize approval gate and ship; {_TERMINAL_REFUSES}"
        )
        assert _next_lines() == [_line(_REQUEUE_FINALIZE, purpose)]
        entries = _next_json()
        assert entries[0]["operator_only"] is True
        assert entries[0]["purpose"] == purpose

    def test_sessionless_finalize_hold_interior_and_terminal(self) -> None:
        review = _blocked(Stage.REVIEW, disposition=FINALIZE_GATE_HELD_DISPOSITION)
        seed_queue([review])
        assert _next_lines() == [
            _line(_REQUEUE_REVIEW, f"{_LABEL}re-run the review stage; {_NO_GATE}")
        ]
        final = _blocked(Stage.FINALIZE, disposition=FINALIZE_GATE_HELD_DISPOSITION)
        seed_queue([final])
        purpose = f"{_LABEL}release the finalize-hold gate and ship; {_NO_GATE}"
        assert _next_lines() == [_line(_REQUEUE_FINALIZE, purpose)]
        assert _next_json()[0]["operator_only"] is True

    def test_off_gate_row(self) -> None:
        task = _blocked(
            Stage.REVIEW, scope_hint="large", disposition="review_pending_approval"
        )
        seed_queue([task], [bound_session_for(task, last_result=_STAGE_COMPLETE)])
        purpose = f"{_LABEL}re-run the review stage; {_NO_GATE}"
        assert _next_lines() == [_line(_REQUEUE_REVIEW, purpose)]
        entries = _next_json()
        assert entries[0]["operator_only"] is True
        assert entries[0]["purpose"] == purpose

    def test_off_pipeline_harden_row(self) -> None:
        task = _blocked(Stage.HARDEN, disposition="approval_gate")
        seed_queue([task], [bound_session_for(task)])
        lines = _next_lines()
        assert lines == [
            _line(_SHOW_CMD, _OFF_PURPOSE.format(stage="harden")),
            _line(_PLAIN_REQUEUE, "re-run the current stage"),
        ]
        assert not [ln for ln in lines if "approve ACM-1" in ln]
        entries = _next_json()
        assert [e["operator_only"] for e in entries] == [False, False]

    def test_off_pipeline_signoff_row(self) -> None:
        task = make_task(
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, stage=Stage.HARDEN
        )
        seed_queue([task])
        assert _next_lines() == [
            _line(_SHOW_CMD, _OFF_PURPOSE.format(stage="harden")),
            _line(_PLAIN_REQUEUE, f"{_LABEL}re-run the current stage"),
        ]
        entries = _next_json()
        assert [e["operator_only"] for e in entries] == [False, True]

    def test_interior_finalize_hold_approve_is_flagged(self) -> None:
        task = _blocked(Stage.REVIEW, disposition=FINALIZE_GATE_HELD_DISPOSITION)
        seed_queue([task], [bound_session_for(task)])
        entries = _next_json()
        assert entries[0]["command"] == f"cw dev-queue approve {_T} -c {_C}"
        assert entries[0]["operator_only"] is True

    def test_schema_carries_the_flag(self) -> None:
        assert "operator_only" in NextCommand.model_json_schema()["properties"]


class TestCancelEcho:
    def test_two_tickets(self) -> None:
        seed_queue([make_task(), make_task(ticket_id="ACM-2")])
        result = invoke_dev_queue("cancel", _T, "ACM-2", "-c", _C)
        assert result.exit_code == 0, result.output
        order = [
            ln
            for ln in stdout_lines(result)
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
        seed_queue([make_task(), make_task()])
        result = invoke_dev_queue("add", _T, "-c", _C)
        assert result.exit_code == 0, result.output
        assert "Warning" not in result.stderr
        assert _ticket_lines(result) == [f"ticket: {_T}"]
        assert "other_rows: 1" in stdout_lines(result)

    def test_add_without_persisted_row(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        seed_queue([])
        monkeypatch.setattr("cw.cli.dev_queue.crud.add_ticket", lambda *_a, **_kw: True)
        with caplog.at_level(logging.DEBUG, logger=_SHOW_LOGGER):
            result = invoke_dev_queue("add", "ACM-9", "-c", _C)
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
            result = invoke_dev_queue("approve", _T, "-c", _C)
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
        seed_queue([make_task()])
        before = _event_count()
        assert invoke_dev_queue("show", _T, "-c", _C).exit_code == 0
        assert invoke_dev_queue("show", _T, "-c", _C, "--json").exit_code == 0
        assert _event_count() == before

    def test_cancel_echo_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        deltas: list[int] = []
        _counting_echo(monkeypatch, "cw.cli.dev_queue.crud.echo_resulting_row", deltas)
        seed_queue([make_task(), make_task(ticket_id="ACM-2")])
        assert invoke_dev_queue("cancel", _T, "ACM-2", "-c", _C).exit_code == 0
        assert deltas == [0, 0]

    def test_approve_echo_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deltas: list[int] = []
        _counting_echo(
            monkeypatch, "cw.cli.dev_queue.approve.echo_resulting_row", deltas
        )
        _seed_review_pending()
        assert invoke_dev_queue("approve", _T, "-c", _C).exit_code == 0
        assert deltas == [0]


class TestRegistration:
    def test_registered(self) -> None:
        ctx = click.Context(main)
        group = main.get_command(ctx, "dev-queue")
        assert isinstance(group, click.Group)
        assert group.get_command(ctx, "show") is not None

    def test_help_names_options(self) -> None:
        result = invoke_dev_queue("show", "--help")
        assert result.exit_code == 0
        assert "-c, --client" in result.output
        assert "--json" in result.output
