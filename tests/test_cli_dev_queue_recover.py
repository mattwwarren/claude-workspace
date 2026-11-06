"""Tests for ``cw dev-queue recover-red-pr`` (GitHub #2945)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw.cli.dev_queue._requeue_echo import echo_requeue_outcome
from cw.dev_queue import save_dev_queue
from cw.dev_queue.lifecycle import transition_task_status
from cw.models import DevQueueStore, OrchestratorEventType, QueueItemStatus, Stage
from cw.pr_feedback import FeedbackSynthesisError
from cw.shipped_pr_recovery import prepare_red_pr_recovery
from tests._adopt_pr_helpers import ADOPT_PR_URL, make_repair_feedback
from tests._shipped_red_helpers import HEAD_SHA, install_red_gh, make_red_row
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    events_of,
    install_acme_client,
    invoke_dev_queue,
    patch_roster,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.dev_queue.red_pr_recovery import RedPrRecovery
    from cw.models import ClientConfig, ReviewFeedback, TicketTask

CANARY = "CANARY-INJECTED-INSTRUCTION"
REQUEUED = OrchestratorEventType.TICKET_REQUEUED


@pytest.fixture(autouse=True)
def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    fb = make_repair_feedback(reviewed_sha=HEAD_SHA, report=f"report {CANARY}")

    def _synth(repo: str, pr_number: int, *, cwd: Path) -> ReviewFeedback:
        return fb

    monkeypatch.setattr("cw.shipped_pr_recovery.synthesize_pr_feedback", _synth)


def _recover() -> tuple[int, str]:
    result = invoke_dev_queue("recover-red-pr", ACME_TICKET, "-c", ACME)
    return result.exit_code, result.output


class TestSuccess:
    def test_regresses_the_row_and_records_the_requeue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head, _req = install_red_gh(monkeypatch, armed=True)
        seed_queue([make_red_row(alerted=True)])

        code, output = _recover()

        assert code == 0, output
        row = queue_row()
        assert row.status is QueueItemStatus.PENDING
        assert row.stage is Stage.IMPL
        assert row.review_feedback is not None
        assert row.red_pr_recovery_sha == HEAD_SHA
        assert head.armed is False
        (event,) = events_of(REQUEUED)
        payload = event.payload
        assert payload["reason"] == "cli_recover_red_pr"
        assert payload["ticket_id"] == ACME_TICKET
        assert payload["client"] == ACME
        assert payload["from_stage"] == "finalize"
        assert payload["to_stage"] == "impl"
        assert payload["regressed"] is True
        assert payload["regress_attempts"] == 1
        assert payload["pr_url"] == ADOPT_PR_URL
        assert payload["head_sha"] == HEAD_SHA
        assert payload["failing_checks"] == ["test (macos-latest, 3.13)"]
        assert payload["was_armed"] is True
        assert payload["disarmed"] is True
        assert (
            f"PR {ADOPT_PR_URL} is now disarmed; finalize re-arms auto-merge after"
            " the recovered commit passes review."
        ) in output

    def test_report_text_is_never_echoed_or_evented(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_red_gh(monkeypatch)
        seed_queue([make_red_row()])

        _code, output = _recover()

        assert CANARY not in output
        assert all(CANARY not in str(e.payload) for e in events_of(REQUEUED))

    def test_client_follows_the_shared_resolver_when_omitted(self) -> None:
        seed_queue([make_red_row()])

        result = invoke_dev_queue("recover-red-pr", ACME_TICKET)

        assert result.exit_code != 0
        assert "Cannot resolve client for ticket" in result.output

    def test_unknown_ticket_is_a_cw_error(self) -> None:
        seed_queue([make_red_row()])

        result = invoke_dev_queue("recover-red-pr", "NOPE-1", "-c", ACME)

        assert result.exit_code != 0
        assert "NOPE-1" in result.output

    def test_second_run_refuses_with_the_exact_not_completed_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_red_gh(monkeypatch)
        seed_queue([make_red_row()])
        assert _recover()[0] == 0

        code, output = _recover()

        assert code != 0
        assert (
            "row is not COMPLETED (status: pending); recover-red-pr only applies"
            " to a shipped PR"
        ) in " ".join(output.split())


class TestRefusals:
    def test_green_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        head, _req = install_red_gh(monkeypatch, required=[])
        seed_queue([make_red_row()])
        before = queue_row()

        code, output = _recover()

        assert code != 0
        assert (
            f"no required check is failing on {ADOPT_PR_URL}; nothing to recover"
            in " ".join(output.split())
        )
        assert queue_row() == before
        assert head.disarm_calls == 0
        assert events_of(REQUEUED) == []

    def test_disarm_failure_leaves_the_row_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_red_gh(monkeypatch, armed=True, disarm="none")
        seed_queue([make_red_row()])
        before = queue_row()

        code, output = _recover()

        assert code != 0
        assert (
            f"could not verify that auto-merge is disarmed on {ADOPT_PR_URL}; the"
            " row is unchanged. Run gh pr merge 7 --disable-auto and retry"
            in " ".join(output.split())
        )
        assert queue_row() == before

    def test_pr_merged_before_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head, _req = install_red_gh(monkeypatch)
        head.state = "MERGED"
        seed_queue([make_red_row()])

        code, output = _recover()

        assert code != 0
        assert "not OPEN" in output

    def test_feedback_head_that_differs_from_the_live_head(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_red_gh(monkeypatch, oid="d" * 40)
        seed_queue([make_red_row()])

        code, output = _recover()

        assert code != 0
        assert "head moved" in output

    def test_synthesis_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_red_gh(monkeypatch)

        def _boom(repo: str, pr_number: int, *, cwd: Path) -> ReviewFeedback:
            msg = "gh unreadable"
            raise FeedbackSynthesisError(msg)

        monkeypatch.setattr("cw.shipped_pr_recovery.synthesize_pr_feedback", _boom)
        seed_queue([make_red_row()])

        code, output = _recover()

        assert code != 0
        assert "gh unreadable" in output


class TestDisarmAudit:
    def test_locked_refusal_after_disarm_is_logged_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        head, _req = install_red_gh(monkeypatch, armed=True)
        seed_queue([make_red_row()])

        def _prepare_then_move(
            task: TicketTask, client_cfg: ClientConfig
        ) -> RedPrRecovery:
            recovery = prepare_red_pr_recovery(task, client_cfg)
            moved = task.model_copy(deep=True)
            transition_task_status(moved, QueueItemStatus.PENDING, unproductive=False)
            save_dev_queue(DevQueueStore(tasks=[moved]))
            return recovery

        monkeypatch.setattr(
            "cw.cli.dev_queue.recover.prepare_red_pr_recovery", _prepare_then_move
        )
        caplog.set_level(logging.WARNING, logger="cw.cli.dev_queue.recover")

        code, output = _recover()

        assert code != 0
        assert (
            f"auto-merge on {ADOPT_PR_URL} is already disarmed but the row was not"
            " changed (row is not COMPLETED); finalize re-arms it after review, so"
            " a retry is safe once the cause is gone"
        ) in " ".join(output.split())
        records = [r for r in caplog.records if r.name == "cw.cli.dev_queue.recover"]
        assert len(records) == 1
        msg = records[0].getMessage()
        for fragment in (
            f"pr_url={ADOPT_PR_URL}",
            "was_armed=True",
            "disarmed=True",
            "outcome=row_write_refused",
        ):
            assert fragment in msg
        assert head.disarm_calls == 1
        assert events_of(REQUEUED) == []

    def test_success_logs_nothing_at_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        install_red_gh(monkeypatch)
        seed_queue([make_red_row()])
        caplog.set_level(logging.WARNING, logger="cw.cli.dev_queue.recover")

        assert _recover()[0] == 0
        assert [r for r in caplog.records if r.name == "cw.cli.dev_queue.recover"] == []


class TestRoster:
    def test_live_session_reaches_the_module_and_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_red_gh(monkeypatch)
        row = make_red_row(session_id="sess-live")
        seed_queue([row], [bound_session_for(row, surface_ref="abcd1234")])
        patch_roster(monkeypatch, {"abcd1234"})

        code, output = _recover()

        assert code != 0
        flat = " ".join(output.split())
        assert "live in the daemon roster" in flat
        assert "already disarmed" in flat
        assert queue_row().status is QueueItemStatus.COMPLETED


class TestHelpAndEcho:
    def test_help_text(self) -> None:
        result = invoke_dev_queue("recover-red-pr", "--help")
        flat = " ".join(result.output.split())
        for fragment in (
            "Recover a shipped PR whose required check failed.",
            "Disarms auto-merge on the PR (verified by read-back from GitHub)",
            "finalize re-arms it after the recovered commit passes review",
            "gh pr merge --auto",
        ):
            assert fragment in flat

    def test_echo_requeue_outcome_defaults_are_unchanged(self) -> None:
        seed_queue([make_red_row(status=QueueItemStatus.PENDING)])
        result: dict[str, str | bool | int] = {
            "from_stage": "finalize",
            "to_stage": "impl",
            "regressed": True,
            "regress_attempts": 1,
            "regress_inferred": False,
            "attempts_reset": False,
            "from_cancelled_applied": False,
            "from_failed_applied": False,
            "from_completed_applied": False,
        }
        echo_requeue_outcome(ACME_TICKET, ACME, result)
        echo_requeue_outcome(
            ACME_TICKET, ACME, result, reason="x", extra_payload={"k": 1}
        )

        default, custom = (e.payload for e in events_of(REQUEUED))
        assert default["reason"] == "cli_regress"
        assert "k" not in default
        assert custom["reason"] == "x"
        assert custom["k"] == 1
        assert set(default) | {"k"} == set(custom)
