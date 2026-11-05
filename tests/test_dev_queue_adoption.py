"""Tests for the RFC 0016 adoption policy leaf and status seam (#2943).

Covers ``cw.dev_queue.adoption`` (conflict check, settle CAS, repair CAS) and
the two adoption hooks in ``cw.dev_queue.lifecycle.transition_task_status``:
R4's ``pr_url`` durability and the pure ``adopted_repin_due`` stamp.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import add_ticket, load_dev_queue, save_dev_queue
from cw.dev_queue.adoption import (
    advance_pin_after_repair,
    apply_settled_pin,
)
from cw.dev_queue.lifecycle import transition_task_status
from cw.exceptions import AdoptionRefusedError
from cw.models import (
    ClientConfig,
    DevQueueStore,
    PrState,
    QueueItemStatus,
    Stage,
)
from tests._adopt_pr_helpers import (
    ADOPT_BRANCH,
    ADOPT_CLIENT,
    ADOPT_PIN,
    ADOPT_PR_URL,
    make_adopted_task,
)
from tests._clients_yaml import write_clients_yaml
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_NEW_PIN = "b" * 40
_OTHER_PR = "https://github.com/acme/widgets/pull/8"
_EXPLICIT_PR = "https://github.com/acme/widgets/pull/99"


@pytest.fixture
def adopt_client_yaml(tmp_path: Path) -> ClientConfig:
    client = ClientConfig(name=ADOPT_CLIENT, workspace_path=tmp_path / "ws")
    write_clients_yaml(client, ensure_workspaces=True)
    return client


def _running(task: TicketTask) -> TicketTask:
    task.status = QueueItemStatus.RUNNING
    return task


class TestPrUrlDurabilitySeam:
    """R4: an adopted row keeps its PR across every transition class."""

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        ],
    )
    def test_terminal_branch_keeps_adopted_pr_url(
        self, status: QueueItemStatus
    ) -> None:
        task = _running(make_adopted_task())
        transition_task_status(task, status, pr_url=None)
        assert task.pr_url == ADOPT_PR_URL

    @pytest.mark.parametrize(
        "status", [QueueItemStatus.PENDING, QueueItemStatus.CANCELLED]
    )
    def test_reset_branch_restores_adopted_pr_url(
        self, status: QueueItemStatus
    ) -> None:
        task = make_adopted_task(status=QueueItemStatus.BLOCKED_ON_USER)
        transition_task_status(task, status)
        assert task.pr_url == ADOPT_PR_URL

    def test_full_cycle_keeps_pr_url(self) -> None:
        task = make_adopted_task()
        transition_task_status(task, QueueItemStatus.RUNNING)
        transition_task_status(task, QueueItemStatus.BLOCKED_ON_USER)
        assert task.pr_url == ADOPT_PR_URL
        transition_task_status(task, QueueItemStatus.PENDING)
        assert task.pr_url == ADOPT_PR_URL
        transition_task_status(task, QueueItemStatus.CANCELLED)
        assert task.pr_url == ADOPT_PR_URL

    def test_explicit_pr_url_still_wins(self) -> None:
        task = _running(make_adopted_task())
        transition_task_status(task, QueueItemStatus.COMPLETED, pr_url=_EXPLICIT_PR)
        assert task.pr_url == _EXPLICIT_PR

    def test_non_adopted_row_still_wipes(self) -> None:
        task = _make_ticket_task(pr_url=_OTHER_PR, status=QueueItemStatus.RUNNING)
        transition_task_status(task, QueueItemStatus.BLOCKED_ON_USER)
        assert task.pr_url is None
        task.pr_url = _OTHER_PR
        transition_task_status(task, QueueItemStatus.PENDING)
        assert task.pr_url is None

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.PENDING,
            QueueItemStatus.CANCELLED,
            QueueItemStatus.BLOCKED_ON_USER,
        ],
    )
    def test_adoption_fields_never_cleared(self, status: QueueItemStatus) -> None:
        task = _running(make_adopted_task())
        transition_task_status(task, status)
        assert task.adopted_head_sha == ADOPT_PIN
        assert task.adopted_pr_url == ADOPT_PR_URL
        assert task.branch == ADOPT_BRANCH


class TestSessionEndMarker:
    """The pure ``adopted_repin_due`` stamp on a RUNNING exit (A2)."""

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.PENDING,
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.CANCELLED,
        ],
    )
    def test_running_exit_sets_flag(self, status: QueueItemStatus) -> None:
        task = _running(make_adopted_task())
        transition_task_status(task, status)
        assert task.adopted_repin_due is True

    def test_running_reassert_does_not_set_flag(self) -> None:
        task = _running(make_adopted_task())
        transition_task_status(task, QueueItemStatus.RUNNING)
        assert task.adopted_repin_due is False

    def test_non_adopted_row_never_flagged(self) -> None:
        task = _make_ticket_task(status=QueueItemStatus.RUNNING)
        transition_task_status(task, QueueItemStatus.PENDING)
        assert task.adopted_repin_due is False

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.PENDING,
            QueueItemStatus.CANCELLED,
            QueueItemStatus.COMPLETED,
        ],
    )
    def test_non_running_exit_leaves_flag_alone(self, status: QueueItemStatus) -> None:
        task = make_adopted_task(
            status=QueueItemStatus.BLOCKED_ON_USER, adopted_repin_due=True
        )
        transition_task_status(task, status)
        assert task.adopted_repin_due is True

    def test_non_running_exit_does_not_set_flag(self) -> None:
        task = make_adopted_task(status=QueueItemStatus.BLOCKED_ON_USER)
        transition_task_status(task, QueueItemStatus.PENDING)
        assert task.adopted_repin_due is False

    def test_routing_core_does_no_gh_or_git_io(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The staged routing core is reachable under sessions_lock: no I/O."""
        from cw.dispatch import _route_staged_decision

        def _forbidden(*_a: object, **_kw: object) -> object:
            msg = "routing core made a gh/git call"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.gh.branch_head_sha_on_origin", _forbidden)
        monkeypatch.setattr("cw._git.is_ancestor", _forbidden)
        monkeypatch.setattr("cw._git.run_git", _forbidden)
        monkeypatch.setattr("cw.dev_queue.adoption.is_ancestor", _forbidden)
        monkeypatch.setattr(
            "cw.dev_queue.adoption.branch_head_sha_on_origin", _forbidden
        )
        task = _running(make_adopted_task(stage=Stage.IMPL))
        save_dev_queue(DevQueueStore(tasks=[task]))
        clients = {
            ADOPT_CLIENT: ClientConfig(name=ADOPT_CLIENT, workspace_path=tmp_path)
        }
        _route_staged_decision(task, "stage_complete", None, clients)
        assert task.status != QueueItemStatus.RUNNING
        assert task.adopted_repin_due is True
        assert task.adopted_head_sha == ADOPT_PIN


class TestConflictCheck:
    """R7: the carried-by-a-live-row check inside ``add_ticket``'s lock."""

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.PENDING,
            QueueItemStatus.RUNNING,
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        ],
    )
    def test_same_pr_on_live_row_refuses(
        self, adopt_client_yaml: ClientConfig, status: QueueItemStatus
    ) -> None:
        save_dev_queue(
            DevQueueStore(tasks=[make_adopted_task(ticket_id="OTHER-1", status=status)])
        )
        with pytest.raises(AdoptionRefusedError, match="already carried by live"):
            add_ticket(make_adopted_task(ticket_id="NEW-1", branch="other/branch"))

    def test_same_branch_different_pr_refuses(
        self, adopt_client_yaml: ClientConfig
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[make_adopted_task(ticket_id="OTHER-1", adopted_pr_url=_OTHER_PR)]
            )
        )
        with pytest.raises(AdoptionRefusedError, match=f"branch '{ADOPT_BRANCH}'"):
            add_ticket(make_adopted_task(ticket_id="NEW-1"))

    def test_non_adopted_row_on_default_branch_refuses(
        self, adopt_client_yaml: ClientConfig
    ) -> None:
        # A plain row for ticket 42 resolves to dev/42; adopting a PR whose
        # head is dev/42 must refuse.
        save_dev_queue(
            DevQueueStore(
                tasks=[_make_ticket_task(ticket_id="42", client=ADOPT_CLIENT)]
            )
        )
        with pytest.raises(AdoptionRefusedError, match="branch 'dev/42'"):
            add_ticket(make_adopted_task(ticket_id="NEW-1", branch="dev/42"))

    @pytest.mark.parametrize(
        "pr_state", [PrState(state="OPEN"), None], ids=["open", "unhydrated"]
    )
    def test_completed_row_with_open_pr_is_live(
        self, adopt_client_yaml: ClientConfig, pr_state: PrState | None
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    make_adopted_task(
                        ticket_id="OTHER-1",
                        status=QueueItemStatus.COMPLETED,
                        pr_state=pr_state,
                    )
                ]
            )
        )
        with pytest.raises(AdoptionRefusedError):
            add_ticket(make_adopted_task(ticket_id="NEW-1"))

    @pytest.mark.parametrize(
        ("status", "pr_state"),
        [
            (QueueItemStatus.COMPLETED, PrState(state="MERGED")),
            (QueueItemStatus.COMPLETED, PrState(state="CLOSED")),
            (QueueItemStatus.CANCELLED, None),
            (QueueItemStatus.FAILED, None),
        ],
    )
    def test_dead_rows_do_not_block(
        self,
        adopt_client_yaml: ClientConfig,
        status: QueueItemStatus,
        pr_state: PrState | None,
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    make_adopted_task(
                        ticket_id="OTHER-1", status=status, pr_state=pr_state
                    )
                ]
            )
        )
        assert add_ticket(make_adopted_task(ticket_id="NEW-1")) is True

    def test_completed_row_without_pr_does_not_block(
        self, adopt_client_yaml: ClientConfig
    ) -> None:
        row = _make_ticket_task(
            ticket_id="OTHER-1",
            client=ADOPT_CLIENT,
            branch=ADOPT_BRANCH,
            status=QueueItemStatus.COMPLETED,
        )
        save_dev_queue(DevQueueStore(tasks=[row]))
        assert add_ticket(make_adopted_task(ticket_id="NEW-1")) is True

    def test_existing_ticket_row_gives_distinct_message(
        self, adopt_client_yaml: ClientConfig
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="NEW-1",
                        client=ADOPT_CLIENT,
                        status=QueueItemStatus.CANCELLED,
                    )
                ]
            )
        )
        with pytest.raises(AdoptionRefusedError, match="already has a row") as exc:
            add_ticket(make_adopted_task(ticket_id="NEW-1"))
        assert "already queued" not in str(exc.value)

    def test_other_client_rows_ignored(self, adopt_client_yaml: ClientConfig) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[make_adopted_task(ticket_id="NEW-1", client="other-client")]
            )
        )
        assert add_ticket(make_adopted_task(ticket_id="NEW-1")) is True

    def test_unknown_client_still_checks(self) -> None:
        save_dev_queue(DevQueueStore(tasks=[make_adopted_task(ticket_id="OTHER-1")]))
        with pytest.raises(AdoptionRefusedError):
            add_ticket(make_adopted_task(ticket_id="NEW-1"))

    def test_non_adopted_add_unchanged(self, adopt_client_yaml: ClientConfig) -> None:
        save_dev_queue(DevQueueStore(tasks=[make_adopted_task(ticket_id="OTHER-1")]))
        plain = _make_ticket_task(ticket_id="OTHER-1", client=ADOPT_CLIENT)
        assert add_ticket(plain) is False


def _seed(**overrides: object) -> None:
    save_dev_queue(
        DevQueueStore(tasks=[make_adopted_task(ticket_id="T-1", **overrides)])
    )


def _row() -> TicketTask:
    return load_dev_queue().tasks[0]


class TestApplySettledPin:
    def test_advances_and_clears(self) -> None:
        _seed(adopted_repin_due=True)
        assert apply_settled_pin(
            ADOPT_CLIENT, "T-1", expected_old=ADOPT_PIN, new_pin=_NEW_PIN
        )
        assert _row().adopted_head_sha == _NEW_PIN
        assert _row().adopted_repin_due is False

    def test_clear_only(self) -> None:
        _seed(adopted_repin_due=True)
        assert apply_settled_pin(
            ADOPT_CLIENT, "T-1", expected_old=ADOPT_PIN, new_pin=ADOPT_PIN
        )
        assert _row().adopted_head_sha == ADOPT_PIN
        assert _row().adopted_repin_due is False

    def test_pin_moved_since_snapshot_is_noop(self) -> None:
        _seed(adopted_repin_due=True, adopted_head_sha=_NEW_PIN)
        assert not apply_settled_pin(
            ADOPT_CLIENT, "T-1", expected_old=ADOPT_PIN, new_pin="c" * 40
        )
        assert _row().adopted_head_sha == _NEW_PIN
        assert _row().adopted_repin_due is True

    def test_flag_already_clear_is_noop(self) -> None:
        _seed(adopted_repin_due=False)
        assert not apply_settled_pin(
            ADOPT_CLIENT, "T-1", expected_old=ADOPT_PIN, new_pin=_NEW_PIN
        )
        assert _row().adopted_head_sha == ADOPT_PIN

    def test_missing_row_is_noop(self) -> None:
        save_dev_queue(DevQueueStore())
        assert not apply_settled_pin(
            ADOPT_CLIENT, "T-1", expected_old=ADOPT_PIN, new_pin=_NEW_PIN
        )

    def test_non_adopted_row_is_noop(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[_make_ticket_task(ticket_id="T-1", client=ADOPT_CLIENT)]
            )
        )
        assert not apply_settled_pin(
            ADOPT_CLIENT, "T-1", expected_old=ADOPT_PIN, new_pin=_NEW_PIN
        )


class TestAdvancePinAfterRepair:
    def test_advances_and_clears_flag(self) -> None:
        _seed(adopted_repin_due=True, status=QueueItemStatus.COMPLETED)
        assert advance_pin_after_repair(
            ADOPT_CLIENT, "T-1", expected_sha=ADOPT_PIN, new_sha=_NEW_PIN
        )
        assert _row().adopted_head_sha == _NEW_PIN
        assert _row().adopted_repin_due is False

    def test_mismatched_pin_is_noop(self) -> None:
        _seed(status=QueueItemStatus.COMPLETED)
        assert not advance_pin_after_repair(
            ADOPT_CLIENT, "T-1", expected_sha="c" * 40, new_sha=_NEW_PIN
        )
        assert _row().adopted_head_sha == ADOPT_PIN

    def test_non_adopted_row_is_noop(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[_make_ticket_task(ticket_id="T-1", client=ADOPT_CLIENT)]
            )
        )
        assert not advance_pin_after_repair(
            ADOPT_CLIENT, "T-1", expected_sha=ADOPT_PIN, new_sha=_NEW_PIN
        )
        assert _row().adopted_head_sha is None

    def test_missing_row_is_noop(self) -> None:
        save_dev_queue(DevQueueStore())
        assert not advance_pin_after_repair(
            ADOPT_CLIENT, "T-1", expected_sha=ADOPT_PIN, new_sha=_NEW_PIN
        )
