"""Tests for the red-PR recovery row write and the IMPL-exit feedback clear.

The clear is source-agnostic (#3018): ``review_feedback`` is dropped whenever a
row leaves IMPL, whether an operator file, adoption repair routing or
``recover-red-pr`` (#2945) attached it.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw import gh
from cw.dev_queue import lifecycle as lc
from cw.dev_queue import load_dev_queue, requeue_ticket
from cw.dev_queue.red_pr_recovery import (
    RedPrRecovery,
    recover_red_pr_ticket,
    red_pr_refusal,
)
from cw.dispatch.pr_gate import _is_review_feedback_pr
from cw.exceptions import RequeueLiveSessionError, RequeueStateError
from cw.executor._repair_entry import is_adopted_repair_entry
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    QueueItemStatus,
    ReviewFeedback,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.review_convergence import ConvergenceLedger
from cw.review_convergence._tip import evaluate_tip
from tests._adopt_pr_helpers import (
    ADOPT_PR_URL,
    make_adopted_repair_task,
    make_repair_feedback,
)
from tests._shipped_red_helpers import make_red_row
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    make_task,
    seed_queue,
)

STAGES = (Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE)
SHA = "b" * 40


def _feedback(sha: str = SHA) -> ReviewFeedback:
    return make_repair_feedback(reviewed_sha=sha)


def _bundle(
    *,
    pr_url: str = ADOPT_PR_URL,
    stages: tuple[Stage, ...] = STAGES,
    backend: str = CLAUDE_NATIVE_BACKEND,
) -> RedPrRecovery:
    return RedPrRecovery(
        pr_url=pr_url,
        repo="acme/widgets",
        pr_number=7,
        head_sha=SHA,
        checks=("test (macos-latest, 3.13)",),
        feedback=_feedback(),
        was_armed=True,
        stages=stages,
        backend=backend,
        lane="default",
    )


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeNativeDaemonClient:
    return install_acme_client(tmp_path, monkeypatch)


def _row():
    return load_dev_queue().tasks[0]


class TestRegress:
    def test_lane_move_during_preparation_refuses_without_regressing(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue([make_red_row(lane="no-review")])
        before = _row()

        with pytest.raises(RequeueStateError, match=r"\(lane changed\)"):
            recover_red_pr_ticket(ACME_TICKET, ACME, _bundle(), native_daemon=daemon)

        after = _row()
        assert after.lane == before.lane
        assert after.status == before.status
        assert after.stage == before.stage
        assert after.pr_url == before.pr_url == ADOPT_PR_URL
        # Recovery already disarmed this PR before locked revalidation refused
        # the lane change, so preserve the owed re-arm marker without applying
        # any of the requested regression.
        assert after.auto_merge_rearm_owed_at is not None
        assert after.auto_merge_rearm_pr_url == ADOPT_PR_URL

    def test_completed_row_becomes_pending_impl_with_feedback(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue(
            [
                make_red_row(
                    alerted=True,
                    review_approved_at=datetime(2026, 11, 1, tzinfo=UTC),
                    review_approver_kind="operator",
                    review_approved_sha="c" * 40,
                    stage_base_ref="abc",
                )
            ]
        )
        bundle = _bundle()

        result = recover_red_pr_ticket(ACME_TICKET, ACME, bundle, native_daemon=daemon)

        row = _row()
        assert result["from_stage"] == "finalize"
        assert result["to_stage"] == "impl"
        assert result["regressed"] is True
        assert row.status is QueueItemStatus.PENDING
        assert row.stage is Stage.IMPL
        assert row.review_feedback == bundle.feedback
        assert row.red_pr_recovery_sha == SHA
        assert row.regressed_into_stage is Stage.IMPL
        assert row.pending_operator_comment is True
        assert row.regress_attempts == 1
        assert row.session_id is None
        assert row.stage_base_ref is None
        assert row.pr_state is None
        assert row.shipped_red is None
        assert row.pr_url == ADOPT_PR_URL
        # Review gate: the approval is revoked, so REVIEW runs again.
        assert row.review_approved_at is None
        assert row.review_approver_kind is None
        assert row.review_approved_sha is None

    def test_adopted_row_keeps_its_pin_and_branch(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue(
            [
                make_adopted_repair_task(
                    client=ACME,
                    ticket_id=ACME_TICKET,
                    status=QueueItemStatus.COMPLETED,
                    stage=Stage.FINALIZE,
                    disposition="shipped",
                    review_feedback=None,
                    session_id="s",
                )
            ]
        )
        before = _row()

        recover_red_pr_ticket(
            ACME_TICKET, ACME, _bundle(pr_url=before.pr_url or ""), native_daemon=daemon
        )

        row = _row()
        assert row.adopted_head_sha == before.adopted_head_sha
        assert row.branch == before.branch
        assert row.adopted_repin_due == before.adopted_repin_due
        assert is_adopted_repair_entry(row) is True

    def test_fix_after_a_clean_round_is_re_reviewed(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue([make_red_row()])
        ledger = ConvergenceLedger(
            ticket_id=ACME_TICKET,
            fork_point="f" * 40,
            max_cycles=3,
            loop_backend="claude-native",
            confirm_backend="none",
            checkpoint_3a_sha="f" * 40,
            status="converged",
            last_clean_sha="a" * 40,
        )
        recover_red_pr_ticket(ACME_TICKET, ACME, _bundle(), native_daemon=daemon)
        store = load_dev_queue()
        row = store.tasks[0]
        lc._advance_task_pointer(row, list(STAGES))

        assert row.stage is Stage.REVIEW
        verdict = evaluate_tip(ledger, head_sha=SHA, origin_tip_sha=SHA)
        assert verdict.decision == "rereview_required"
        assert verdict.reason == "commits_after_clean_round"

    def test_pipeline_is_impl_review_finalize(self) -> None:
        row = make_red_row(stage=Stage.IMPL)
        lc._advance_task_pointer(row, list(STAGES))
        assert row.stage is Stage.REVIEW
        lc._advance_task_pointer(row, list(STAGES))
        assert row.stage is Stage.FINALIZE

    def test_pr_gate_releases_the_regressed_row_only_with_feedback(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue([make_red_row()])
        recover_red_pr_ticket(ACME_TICKET, ACME, _bundle(), native_daemon=daemon)
        row = _row()

        assert _is_review_feedback_pr(row, ADOPT_PR_URL) is True
        row.review_feedback = None
        assert _is_review_feedback_pr(row, ADOPT_PR_URL) is False


class TestConcurrency:
    def test_row_no_longer_completed_is_refused_with_the_disarmed_message(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue([make_red_row(status=QueueItemStatus.PENDING)])

        with pytest.raises(RequeueStateError) as exc:
            recover_red_pr_ticket(ACME_TICKET, ACME, _bundle(), native_daemon=daemon)

        assert str(exc.value) == (
            f"auto-merge on {ADOPT_PR_URL} is already disarmed but the row was not"
            " changed (row is not COMPLETED); finalize re-arms it after review, so"
            " a retry is safe once the cause is gone"
        )
        assert _row().status is QueueItemStatus.PENDING

    def test_pr_url_changed_is_refused(self, daemon: FakeNativeDaemonClient) -> None:
        seed_queue([make_red_row(pr_url="https://github.com/acme/widgets/pull/9")])

        with pytest.raises(RequeueStateError, match=r"\(pr_url changed\)"):
            recover_red_pr_ticket(ACME_TICKET, ACME, _bundle(), native_daemon=daemon)

        assert _row().status is QueueItemStatus.COMPLETED

    def test_locked_step_applies_the_shared_refusal_rules(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue([make_red_row(disposition="merge_pending")])

        with pytest.raises(RequeueStateError, match="not 'shipped'"):
            recover_red_pr_ticket(ACME_TICKET, ACME, _bundle(), native_daemon=daemon)


class TestLiveSession:
    def test_live_session_is_refused_before_any_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = install_acme_client(tmp_path, monkeypatch)
        row = make_red_row(session_id="sess-live")
        daemon._live.add("abcd1234")
        seed_queue([row], [bound_session_for(row, surface_ref="abcd1234")])

        with pytest.raises(RequeueLiveSessionError):
            recover_red_pr_ticket(ACME_TICKET, ACME, _bundle(), native_daemon=daemon)

        assert _row().status is QueueItemStatus.COMPLETED

    def test_unreadable_roster_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = install_acme_client(tmp_path, monkeypatch)
        daemon.roster_unreadable = True
        seed_queue([make_red_row()])

        with pytest.raises(RequeueLiveSessionError):
            recover_red_pr_ticket(ACME_TICKET, ACME, _bundle(), native_daemon=daemon)


class TestRefusalRules:
    @pytest.mark.parametrize(
        ("kwargs", "stages", "backend", "fragment"),
        [
            (
                {"status": QueueItemStatus.PENDING},
                STAGES,
                "claude-native",
                "not COMPLETED",
            ),
            ({"disposition": "signoff_gate"}, STAGES, "claude-native", "not 'shipped'"),
            ({"pr_url": "x"}, STAGES, "claude-native", "parseable"),
            (
                {},
                (Stage.PLAN, Stage.REVIEW, Stage.FINALIZE),
                "claude-native",
                "no impl",
            ),
            ({}, (Stage.IMPL, Stage.FINALIZE), "claude-native", "no review stage"),
            ({}, STAGES, "opencode", "#2983"),
            ({}, STAGES, "local", "#2983"),
        ],
    )
    def test_refusals(
        self,
        kwargs: dict[str, object],
        stages: tuple[Stage, ...],
        backend: str,
        fragment: str,
    ) -> None:
        msg = red_pr_refusal(make_red_row(**kwargs), stages, backend)
        assert msg is not None
        assert fragment in msg

    def test_eligible_row_has_no_refusal(self) -> None:
        assert red_pr_refusal(make_red_row(), STAGES, CLAUDE_NATIVE_BACKEND) is None


@pytest.fixture(params=["operator", "adopted", "recovered"])
def impl_row(request: pytest.FixtureRequest) -> TicketTask:
    """A RUNNING IMPL row carrying feedback from each source that can set it."""
    if request.param == "operator":
        return make_task(
            stage=Stage.IMPL,
            status=QueueItemStatus.RUNNING,
            review_feedback=make_repair_feedback(),
        )
    if request.param == "adopted":
        return make_adopted_repair_task(status=QueueItemStatus.RUNNING)
    return make_red_row(
        stage=Stage.IMPL,
        status=QueueItemStatus.RUNNING,
        review_feedback=_feedback(),
        red_pr_recovery_sha=SHA,
    )


class TestImplExitClear:
    def test_helper_audits_before_clearing_feedback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = make_task(
            stage=Stage.IMPL,
            review_feedback=make_repair_feedback(),
            red_pr_recovery_sha=SHA,
        )
        expected_feedback = row.review_feedback.model_dump(mode="json")
        captured: list[tuple[object, dict[str, object], str | None]] = []

        def capture(
            event_type: object,
            payload: dict[str, object],
            *,
            correlation_id: str | None = None,
        ) -> None:
            assert row.review_feedback is not None
            captured.append((event_type, payload, correlation_id))

        monkeypatch.setattr(lc, "record_event", capture)
        lc._consume_red_recovery_evidence(row, Stage.IMPL, Stage.REVIEW)

        assert len(captured) == 1
        event_type, payload, correlation_id = captured[0]
        assert event_type.value == "task.transition"
        assert payload["ticket_id"] == row.ticket_id
        assert payload["client"] == row.client
        assert payload["source"] == "recover-red-pr"
        assert payload["old_stage"] == Stage.IMPL
        assert payload["new_stage"] == Stage.REVIEW
        assert payload["reason"] == "impl_exit"
        assert payload["transition"] == "review_feedback_cleared"
        assert payload["review_feedback"] == expected_feedback
        assert payload["red_pr_recovery_sha"] == SHA
        assert correlation_id == row.ticket_id
        assert row.review_feedback is None
        assert row.red_pr_recovery_sha is None

    def test_helper_keeps_feedback_when_audit_write_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = make_task(stage=Stage.IMPL, review_feedback=make_repair_feedback())

        def fail(*_: object, **__: object) -> None:
            message = "event inbox unavailable"
            raise OSError(message)

        monkeypatch.setattr(lc, "record_event", fail)
        with pytest.raises(OSError, match="event inbox unavailable"):
            lc._consume_red_recovery_evidence(row, Stage.IMPL, Stage.REVIEW)

        assert row.review_feedback is not None

    def test_helper_clears_only_when_leaving_impl(self) -> None:
        row = make_task(stage=Stage.IMPL, review_feedback=make_repair_feedback())
        lc._consume_red_recovery_evidence(row, Stage.REVIEW, Stage.FINALIZE)
        assert row.review_feedback is not None
        lc._consume_red_recovery_evidence(row, Stage.IMPL, Stage.IMPL)
        assert row.review_feedback is not None
        lc._consume_red_recovery_evidence(row, Stage.IMPL, Stage.REVIEW)
        assert row.review_feedback is None

    def test_replaced_feedback_is_also_cleared(self) -> None:
        row = make_red_row(
            stage=Stage.IMPL,
            status=QueueItemStatus.RUNNING,
            review_feedback=_feedback("d" * 40),
            red_pr_recovery_sha=SHA,
        )
        lc._consume_red_recovery_evidence(row, Stage.IMPL, Stage.REVIEW)
        assert row.review_feedback is None
        assert row.red_pr_recovery_sha is None

    def test_helper_clears_stray_marker_without_feedback(self) -> None:
        row = make_task(stage=Stage.IMPL, red_pr_recovery_sha=SHA)
        assert row.review_feedback is None
        lc._consume_red_recovery_evidence(row, Stage.IMPL, Stage.REVIEW)
        assert row.red_pr_recovery_sha is None

    def test_row_that_walks_to_finalize_then_self_heals_has_no_feedback(
        self, impl_row: TicketTask
    ) -> None:
        lc._advance_task_pointer(impl_row, list(STAGES))
        lc._advance_task_pointer(impl_row, list(STAGES))
        assert impl_row.stage is Stage.FINALIZE
        lc._stage_regress(impl_row, Stage.IMPL)
        assert impl_row.review_feedback is None

    def test_feedback_attached_before_a_finalize_regress_survives_it(self) -> None:
        # Ordering contract: requeue --regress attaches the fresh payload
        # BEFORE the stage move, and old_stage is FINALIZE, so it is kept.
        row = make_task(
            stage=Stage.FINALIZE,
            status=QueueItemStatus.COMPLETED,
            review_feedback=make_repair_feedback(),
        )
        lc._stage_regress(row, Stage.IMPL)
        assert row.stage is Stage.IMPL
        assert row.review_feedback is not None

    def test_transition_clears_latch_but_not_marker(self) -> None:
        row = make_red_row(alerted=True, red_pr_recovery_sha=SHA)
        lc.transition_task_status(row, QueueItemStatus.PENDING, unproductive=False)
        assert row.shipped_red is None
        assert row.red_pr_recovery_sha == SHA

    def test_same_status_reassert_clears_latch(self) -> None:
        row = make_red_row(alerted=True)
        lc.transition_task_status(row, QueueItemStatus.COMPLETED, unproductive=False)
        assert row.shipped_red is None


class TestEveryImplExitRoute:
    """One test per ``task.stage =`` writer (ARCHITECTURE.md principle 11)."""

    @staticmethod
    def _assert_cleared(row: TicketTask) -> None:
        assert row.review_feedback is None
        assert row.red_pr_recovery_sha is None

    def test_advance_task_pointer(self, impl_row: TicketTask) -> None:
        lc._advance_task_pointer(impl_row, list(STAGES))
        self._assert_cleared(impl_row)

    def test_advance_stage_to_review(self, impl_row: TicketTask) -> None:
        lc._advance_stage(impl_row, list(STAGES), Stage.REVIEW)
        self._assert_cleared(impl_row)

    def test_advance_stage_to_finalize(self, impl_row: TicketTask) -> None:
        lc._advance_stage(impl_row, list(STAGES), Stage.FINALIZE)
        self._assert_cleared(impl_row)

    def test_advance_stage_same_stage_keeps(self, impl_row: TicketTask) -> None:
        marker = impl_row.red_pr_recovery_sha
        lc._advance_stage(impl_row, list(STAGES), Stage.IMPL)
        assert impl_row.review_feedback is not None
        assert impl_row.red_pr_recovery_sha == marker

    def test_stamp_salvage_stage(self, impl_row: TicketTask) -> None:
        lc._stamp_salvage_stage(impl_row)
        self._assert_cleared(impl_row)

    def test_stage_regress_out_of_impl(self, impl_row: TicketTask) -> None:
        lc._stage_regress(impl_row, Stage.PLAN)
        self._assert_cleared(impl_row)

    def test_requeue_stage_review_on_a_parked_recovered_row(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        parked = make_red_row(
            stage=Stage.IMPL,
            status=QueueItemStatus.BLOCKED_ON_USER,
            review_feedback=_feedback(),
            red_pr_recovery_sha=SHA,
            session_id=None,
        )
        seed_queue([parked])

        requeue_ticket(ACME_TICKET, ACME, "review", native_daemon=daemon)

        row = _row()
        assert row.stage is Stage.REVIEW
        assert row.review_feedback is None
        assert row.red_pr_recovery_sha is None

    def test_requeue_same_stage_impl_keeps_the_evidence(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        parked = make_red_row(
            stage=Stage.IMPL,
            status=QueueItemStatus.BLOCKED_ON_USER,
            review_feedback=_feedback(),
            red_pr_recovery_sha=SHA,
            session_id=None,
        )
        seed_queue([parked])

        requeue_ticket(ACME_TICKET, ACME, "impl", native_daemon=daemon)

        row = _row()
        assert row.stage is Stage.IMPL
        assert row.review_feedback is not None
        assert row.red_pr_recovery_sha == SHA

    def test_requeue_stage_review_on_a_parked_operator_row(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue(
            [
                make_task(
                    stage=Stage.IMPL,
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id=None,
                    review_feedback=make_repair_feedback(),
                )
            ]
        )

        requeue_ticket(ACME_TICKET, ACME, "review", native_daemon=daemon)

        row = _row()
        assert row.stage is Stage.REVIEW
        assert row.review_feedback is None

    def test_requeue_same_stage_impl_keeps_operator_feedback(
        self, daemon: FakeNativeDaemonClient
    ) -> None:
        seed_queue(
            [
                make_task(
                    stage=Stage.IMPL,
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id=None,
                    review_feedback=make_repair_feedback(),
                )
            ]
        )

        requeue_ticket(ACME_TICKET, ACME, "impl", native_daemon=daemon)

        row = _row()
        assert row.stage is Stage.IMPL
        assert row.review_feedback is not None

    def test_the_verbs_own_finalize_to_impl_regress_does_not_clear(self) -> None:
        row = make_red_row(review_feedback=_feedback(), red_pr_recovery_sha=SHA)
        lc._stage_regress(row, Stage.IMPL)
        assert row.review_feedback is not None

    def test_stage_writers_are_exactly_the_known_set(self) -> None:
        """A new ``.stage =`` writer must be handled (or allow-listed) here."""
        src = Path(lc.__file__).parents[1]
        expected = {
            "dev_queue/lifecycle.py": {
                "_advance_stage",
                "_stamp_salvage_stage",
                "_advance_task_pointer",
                "_stage_regress",
            },
            "dispatch/claim/spawn.py": {"_apply_plan_bypass_if_available"},
        }
        found: dict[str, set[str]] = {}
        for path in src.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for node in ast.walk(fn):
                    targets = node.targets if isinstance(node, ast.Assign) else []
                    if any(
                        isinstance(t, ast.Attribute) and t.attr == "stage"
                        for t in targets
                    ):
                        found.setdefault(str(path.relative_to(src)), set()).add(fn.name)
        assert found == expected


def test_gh_namespace_is_not_touched_by_the_row_layer() -> None:
    assert gh.fetch_pr_head is not None
