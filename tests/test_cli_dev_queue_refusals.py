"""CLI end-to-end: every dev-queue refusal names the legal next command (#2692).

Covers the ``approve`` refusals through Click, the idempotent PENDING
``requeue`` no-op (with its read-only live-session note), inferred
``--regress``, and the ``unblock``/``move`` refusals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.cli.dev_queue._settle import SettleOutcome
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.models import OrchestratorEventType, QueueItemStatus, SessionStatus, Stage
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    CASES,
    bound_session_for,
    install_acme_client,
    invoke_dev_queue,
    make_blocked,
    make_task,
    patch_roster,
    seed_queue,
    stdout_lines,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.native_daemon import FakeNativeDaemonClient

_APPROVE = "cw dev-queue approve ACM-1 -c acme"
_NOOP_LINE = "Ticket ACM-1 (acme) is already PENDING at {stage}; nothing to requeue."
_LIVE_NOTE = (
    "note: session sess-live (acme/auto-dev/ACM-1) for this ticket is live in the"
    " daemon roster; this no-op stopped nothing. To close it, run:"
    " `cw spawn close sess-live --requeue`."
)
_ROSTER_NOTE = (
    "note: the daemon roster could not be read, so a live session for this"
    " ticket cannot be ruled out; this no-op stopped nothing. If one is known,"
    " close it with `cw spawn close <sid> --requeue`."
)
_REFUSED_BY_APPROVE = tuple(
    sorted(
        case_id
        for case_id, case in CASES.items()
        if _APPROVE not in case.expected and not case_id.startswith("order_")
    )
)


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeNativeDaemonClient:
    return install_acme_client(tmp_path, monkeypatch)


def _requeued_events() -> list[object]:
    return list(read_events(event_types=[OrchestratorEventType.TICKET_REQUEUED]))


class TestApproveRefusals:
    @pytest.mark.parametrize("case_id", _REFUSED_BY_APPROVE)
    def test_refusal_names_a_command(self, case_id: str) -> None:
        case = CASES[case_id]
        session = case.session()
        seed_queue([case.task], [session] if session is not None else [])
        result = invoke_dev_queue("approve", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 1
        assert "Error: Cannot approve ticket 'ACM-1'" in result.stderr
        assert "`cw " in result.stderr

    def test_sessionless_held_finalize(self) -> None:
        seed_queue([CASES["rule0_finalize_held_no_session"].task])
        result = invoke_dev_queue("approve", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 1
        assert "human-kept gate" in result.stderr
        assert "operator-only; not for an agent to run" in result.stderr
        assert "cw dev-queue requeue" not in result.stderr
        assert "Use 'requeue'" not in result.stderr

    def test_override_success_echo_carries_the_human_caveat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue(
            [make_blocked(stage=Stage.REVIEW, blocked_reason=CODEX_MUST_FIX_FINDINGS)]
        )

        def _fake_override(
            ticket_id: str, client: str, _reason: str, **_kw: object
        ) -> dict[str, object]:
            return {
                "ticket_id": ticket_id,
                "client": client,
                "stage": "review",
                "actor": "alice",
                "reviewed_sha": "a" * 40,
                "finding_ids": [],
            }

        monkeypatch.setattr(
            "cw.cli.dev_queue.approve.approve_must_fix_override_ticket",
            _fake_override,
        )
        result = invoke_dev_queue(
            "approve", ACME_TICKET, "-c", ACME, "--override-must-fix", "--reason", "x"
        )
        assert result.exit_code == 0, result.output
        assert stdout_lines(result)[0].endswith(
            " If the ticket or lane has a finalize hold, operator signoff or"
            " scope_hint: large, this step is the human's."
        )


class TestIdempotentRequeue:
    def test_pending_row_is_a_noop(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        before = load_dev_queue().model_dump()
        result = invoke_dev_queue("requeue", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 0, result.output
        assert stdout_lines(result)[0] == _NOOP_LINE.format(stage="plan")
        assert "note:" not in result.stderr
        assert _requeued_events() == []
        assert load_dev_queue().model_dump() == before

    def test_same_stage_is_a_noop(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING, stage=Stage.REVIEW)])
        result = invoke_dev_queue(
            "requeue", ACME_TICKET, "-c", ACME, "--stage", "review"
        )
        assert result.exit_code == 0, result.output
        assert stdout_lines(result)[0] == _NOOP_LINE.format(stage="review")

    def test_live_session_is_named_and_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = make_task(status=QueueItemStatus.PENDING, session_id="sess-live")
        seed_queue([task], [bound_session_for(task, surface_ref="abcd1234")])
        fake = patch_roster(monkeypatch, {"abcd1234"})
        closes: list[tuple[object, ...]] = []

        def _close_spy(*args: object, **_kw: object) -> list[str]:
            closes.append(args)
            return []

        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.close_routed_result_sessions_for_ticket",
            _close_spy,
        )
        result = invoke_dev_queue("requeue", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 0, result.output
        assert _LIVE_NOTE in result.stderr.splitlines()
        assert fake.stop_calls == []
        assert closes == []
        assert _requeued_events() == []
        state_session = next(s for s in load_state().sessions if s.id == "sess-live")
        assert state_session.status == SessionStatus.ACTIVE

    def test_unreadable_roster_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        patch_roster(monkeypatch, None)
        result = invoke_dev_queue("requeue", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 0, result.output
        assert _ROSTER_NOTE in result.stderr.splitlines()

    def test_race_produced_already_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = make_task(status=QueueItemStatus.PENDING, session_id="sess-live")
        seed_queue([task], [bound_session_for(task, surface_ref="abcd1234")])
        stale = task.model_copy(update={"status": QueueItemStatus.BLOCKED_ON_USER})
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.settle_lagging_row",
            lambda *_a: SettleOutcome(row=stale, routed=False),
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.precheck_requeue", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.dev_queue.requeue._refuse_if_live_session", lambda *_a, **_kw: None
        )
        patch_roster(monkeypatch, {"abcd1234"})
        before = load_dev_queue().model_dump()
        result = invoke_dev_queue("requeue", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 0, result.output
        assert stdout_lines(result)[0] == _NOOP_LINE.format(stage="plan")
        assert _LIVE_NOTE in result.stderr.splitlines()
        assert _requeued_events() == []
        assert load_dev_queue().model_dump() == before

    def test_pending_with_a_backward_stage_refuses(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING, stage=Stage.REVIEW)])
        result = invoke_dev_queue("requeue", ACME_TICKET, "-c", ACME, "--stage", "plan")
        assert result.exit_code == 1
        assert "Run: `cw dev-queue wait ACM-1 -c acme`" in result.stderr
        assert "`cw dev-queue add ACM-1 -c acme --stage plan`." in result.stderr


class TestInferredRegress:
    @pytest.mark.parametrize(
        ("row_stage", "target", "evidence"),
        [
            (
                Stage.REVIEW,
                "plan",
                "  Regressing into plan cleared the plan approval (plan_approved_at,"
                " plan_approved_fingerprint); approve the new plan draft again.",
            ),
            (
                Stage.FINALIZE,
                "review",
                "  Regressing into review cleared any recorded MUST_FIX override;"
                " record a new one against the fresh verdict if you still need it.",
            ),
            (Stage.REVIEW, "impl", None),
        ],
        ids=["plan", "review", "impl"],
    )
    def test_backward_stage_is_inferred_and_announced(
        self, row_stage: Stage, target: str, evidence: str | None
    ) -> None:
        seed_queue([make_blocked(stage=row_stage)])
        result = invoke_dev_queue("requeue", ACME_TICKET, "-c", ACME, "--stage", target)
        assert result.exit_code == 0, result.output
        lines = stdout_lines(result)
        expected = [
            f"Inferred --regress: '{target}' is behind '{row_stage.value}'.",
            "  regress_attempts is now 1.",
        ]
        if evidence is not None:
            expected.append(evidence)
        expected.append(
            f"Requeued ACM-1 (acme): {row_stage.value} -> {target} (PENDING)"
        )
        assert lines[: len(expected)] == expected
        events = read_events(event_types=[OrchestratorEventType.TICKET_REQUEUED])
        assert events[-1].payload["reason"] == "cli_regress"

    def test_explicit_regress_prints_no_inference(self) -> None:
        seed_queue([make_blocked(stage=Stage.REVIEW)])
        result = invoke_dev_queue(
            "requeue", ACME_TICKET, "-c", ACME, "--stage", "impl", "--regress"
        )
        assert result.exit_code == 0, result.output
        assert "Inferred" not in result.stdout
        assert (
            stdout_lines(result)[0] == "Requeued ACM-1 (acme): review -> impl (PENDING)"
        )


class TestUnblockAndMoveRefusals:
    def test_unblock_on_a_gate_names_approve(self) -> None:
        case = CASES["rule9_review_gate"]
        seed_queue(
            [case.task],
            [bound_session_for(case.task, last_result=case.session_last_result)],
        )
        result = invoke_dev_queue("unblock", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 1
        assert "unblock applies to salvage parks only" in result.stderr
        assert f"Run: `{_APPROVE}`" in result.stderr

    def test_move_on_a_blocked_row(self) -> None:
        seed_queue([make_blocked(stage=Stage.PLAN)])
        result = invoke_dev_queue("move", ACME_TICKET, "-c", ACME, "--to", "fast")
        assert result.exit_code == 1
        assert "Run: `cw dev-queue requeue ACM-1 -c acme`" in result.stderr
        assert "Once the row is PENDING again, retry move." in result.stderr
