"""Locked cores of ``cw dev-queue prerequisite`` (#3791).

Pure and locked: no gh, no git and no subprocess under the queue lock. The
verb verifies the PR before calling :func:`declare_prerequisite`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result import DEPENDENCY_UNMERGED_BLOCKER_REASON
from cw.config import dev_queue_file
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dev_queue.merge_prerequisites import (
    MAX_MERGE_PREREQUISITES,
    check_declarable,
    declare_prerequisite,
    has_prerequisite,
    remove_prerequisite,
)
from cw.exceptions import CwError
from cw.models import (
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    WatchedPr,
)
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION, TERMINAL_QUEUE_STATUSES
from cw.models.pr_ref import PrRef
from tests._shared.merge_prerequisite_builders import (
    DECLARED_AT,
    MP_CLIENT,
    MP_SLUG,
    MP_TICKET,
    pending_row,
    prerequisite_entry,
    seed_rows,
    stored_row,
)
from tests._ticket_view_helpers import events_of
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from cw.models import TicketTask

_REF_9 = PrRef(number=9)
_REF_10 = PrRef(number=10)


def _declare(ref: PrRef = _REF_9) -> object:
    return declare_prerequisite(
        MP_TICKET, MP_CLIENT, ref, repo=MP_SLUG, actor="operator"
    )


def _held_on(number: int, **overrides: object) -> TicketTask:
    fields: dict[str, object] = {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": DEPENDENCY_HOLD_DISPOSITION,
        "blocked_reason": DEPENDENCY_UNMERGED_BLOCKER_REASON,
        "blocked_on_pr": number,
    }
    fields.update(overrides)
    return pending_row(**fields)


class TestCheckDeclarable:
    @pytest.mark.parametrize("status", sorted(TERMINAL_QUEUE_STATUSES))
    def test_refuses_terminal_rows(self, status: QueueItemStatus) -> None:
        with pytest.raises(CwError) as exc:
            check_declarable(pending_row(status=status))

        assert str(exc.value) == (
            f"ticket {MP_TICKET} in client {MP_CLIENT} is {status.value};"
            " a merge prerequisite applies only to a row that can still dispatch"
        )

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.PENDING,
            QueueItemStatus.RUNNING,
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        ],
    )
    def test_accepts_dispatchable_rows_and_never_changes_status(
        self, status: QueueItemStatus
    ) -> None:
        row = pending_row(
            status=status, session_id="sess-1", disposition="x", blocked_on_pr=4
        )
        seed_rows(row)

        _declare()

        after = stored_row()
        assert after.status == status
        assert after.disposition == "x"
        assert after.session_id == "sess-1"
        assert after.blocked_on_pr == PrRef(number=4)
        assert [e.pr for e in after.merge_prerequisites] == [_REF_9]


class TestDeclare:
    def test_new_entry(self) -> None:
        seed_rows(pending_row())

        result = _declare()

        assert result == result.__class__(
            ticket_id=MP_TICKET,
            client=MP_CLIENT,
            pr_number=9,
            outcome="declared",
            released=False,
            restamped_to=None,
        )
        [entry] = stored_row().merge_prerequisites
        assert entry.pr == _REF_9
        assert entry.repo == MP_SLUG
        assert entry.actor == "operator"
        assert entry.declared_at is not None
        assert entry.satisfied_at is None

    def test_second_pr_appends_in_order(self) -> None:
        seed_rows(pending_row())

        _declare(_REF_9)
        _declare(_REF_10)

        assert [e.pr.number for e in stored_row().merge_prerequisites] == [9, 10]

    def test_same_pr_twice_is_unchanged_and_writes_nothing(self) -> None:
        seed_rows(pending_row())
        _declare()
        before = dev_queue_file().read_bytes()

        result = _declare()

        assert result.outcome == "unchanged"
        assert dev_queue_file().read_bytes() == before

    def test_redeclaring_a_satisfied_entry_is_unchanged(self) -> None:
        seed_rows(
            pending_row(merge_prerequisites=[prerequisite_entry(9, satisfied=True)])
        )

        assert _declare().outcome == "unchanged"
        [entry] = stored_row().merge_prerequisites
        assert entry.satisfied_at == DECLARED_AT

    def test_cap_refuses_before_mutating(self) -> None:
        entries = [prerequisite_entry(n) for n in range(1, MAX_MERGE_PREREQUISITES + 1)]
        seed_rows(pending_row(merge_prerequisites=entries))
        before = stored_row().model_dump()

        with pytest.raises(CwError, match="already has 8 merge prerequisites"):
            _declare(PrRef(number=99))

        assert stored_row().model_dump() == before

    @pytest.mark.parametrize("field", ["pr_url", "adopted_pr_url"])
    def test_refuses_the_rows_own_pr(self, field: str) -> None:
        seed_rows(pending_row(**{field: "https://github.com/foo/bar/pull/9"}))
        before = stored_row().model_dump()

        with pytest.raises(CwError, match="is ticket MP-1's own PR"):
            _declare()

        assert stored_row().model_dump() == before

    def test_own_pr_in_another_repo_is_not_the_same_pr(self) -> None:
        seed_rows(pending_row(pr_url="https://github.com/other/repo/pull/9"))

        assert _declare().outcome == "declared"

    def test_refuses_a_pr_another_client_watches(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[pending_row()],
                watched_prs=[
                    WatchedPr(
                        pr_url="https://github.com/foo/bar/pull/9",
                        repo=MP_SLUG,
                        pr_number=9,
                        client="other-client",
                        source="dependency_hold",
                    )
                ],
            )
        )
        before = stored_row().model_dump()

        with pytest.raises(CwError, match="already watched by client other-client"):
            _declare()

        assert stored_row().model_dump() == before

    def test_has_prerequisite(self) -> None:
        row = pending_row(merge_prerequisites=[prerequisite_entry(9)])

        assert has_prerequisite(row, _REF_9)
        assert not has_prerequisite(row, _REF_10)


class TestRemove:
    def test_unknown_pr_raises(self) -> None:
        seed_rows(pending_row())

        with pytest.raises(CwError, match="has no merge prerequisite on PR #9"):
            remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_9)

    def test_entry_on_a_non_parked_row_is_removed_only(self) -> None:
        seed_rows(pending_row(merge_prerequisites=[prerequisite_entry(9)]))
        before = stored_row().model_dump()

        result = remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_9)

        assert result.outcome == "removed"
        assert not result.released
        assert result.restamped_to is None
        after = stored_row().model_dump()
        assert after.pop("merge_prerequisites") == []
        before.pop("merge_prerequisites")
        assert after == before

    def test_last_entry_releases_a_sessionless_hold_on_it(self) -> None:
        seed_rows(_held_on(9, merge_prerequisites=[prerequisite_entry(9)]))

        result = remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_9)

        assert result.released
        row = stored_row()
        assert row.status == QueueItemStatus.PENDING
        assert row.blocked_on_pr is None
        assert row.session_id is None
        assert row.merge_prerequisites == []
        transitions = events_of(OrchestratorEventType.TASK_TRANSITION)
        assert transitions[-1].payload["new_status"] == QueueItemStatus.PENDING.value

    def test_one_of_two_restamps_the_hold_to_the_next(self) -> None:
        row = _held_on(
            9,
            merge_prerequisites=[prerequisite_entry(9), prerequisite_entry(10)],
            stale_gate_detected_at=DECLARED_AT,
        )
        seed_rows(row)

        result = remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_9)

        assert not result.released
        assert result.restamped_to == 10
        after = stored_row()
        assert after.status == QueueItemStatus.BLOCKED_ON_USER
        assert after.disposition == DEPENDENCY_HOLD_DISPOSITION
        assert after.blocked_on_pr == _REF_10
        assert after.stale_gate_detected_at is None

    def test_removing_an_entry_that_is_not_the_current_park(self) -> None:
        seed_rows(
            _held_on(
                9, merge_prerequisites=[prerequisite_entry(9), prerequisite_entry(10)]
            )
        )

        result = remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_10)

        assert not result.released
        assert result.restamped_to is None
        after = stored_row()
        assert after.status == QueueItemStatus.BLOCKED_ON_USER
        assert after.blocked_on_pr == _REF_9

    def test_worker_park_on_the_same_pr_is_not_released(self) -> None:
        seed_rows(
            _held_on(
                9, session_id="sess-1", merge_prerequisites=[prerequisite_entry(9)]
            )
        )

        result = remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_9)

        assert not result.released
        after = stored_row()
        assert after.status == QueueItemStatus.BLOCKED_ON_USER
        assert after.blocked_on_pr == _REF_9
        assert after.session_id == "sess-1"

    def test_removing_a_satisfied_entry_changes_no_park(self) -> None:
        seed_rows(
            _held_on(9, merge_prerequisites=[prerequisite_entry(9, satisfied=True)])
        )

        result = remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_9)

        assert not result.released
        after = stored_row()
        assert after.status == QueueItemStatus.BLOCKED_ON_USER
        assert after.blocked_on_pr == _REF_9

    def test_a_different_disposition_on_the_same_pr_is_not_released(self) -> None:
        seed_rows(
            _held_on(
                9,
                disposition="merge_gate_blocked",
                merge_prerequisites=[prerequisite_entry(9)],
            )
        )

        assert not remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_9).released
        assert stored_row().status == QueueItemStatus.BLOCKED_ON_USER


def test_cores_make_no_gh_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGhCli().install(monkeypatch)
    seed_rows(pending_row())

    _declare()
    remove_prerequisite(MP_TICKET, MP_CLIENT, _REF_9)

    assert fake.records == []
    assert load_dev_queue().tasks[0].merge_prerequisites == []
