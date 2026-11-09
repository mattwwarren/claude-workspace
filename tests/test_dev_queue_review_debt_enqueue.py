"""Tests for :mod:`cw.dev_queue.review_debt_enqueue` (#3045, RFC 0017 D-8).

Hermetic: the autouse launch guard fails any gh or git call, and the autouse
``tmp_config_dir`` isolates the inbox, the cursor files and ``clients.yaml``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

import cw.dev_queue.crud as crud_mod
import cw.dev_queue.review_debt_enqueue as mod
from cw import _lock_guard
from cw.dev_queue.crud import StrictAddResult, add_ticket_if_absent
from cw.dev_queue.review_debt_enqueue import (
    DEBT_AUTOENQUEUE_PRIORITY,
    enqueue_review_debt_for_merged_prs,
)
from cw.dev_queue.storage import load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.exceptions import (
    DEV_QUEUE_LOCK_NAME,
    LaneNotFoundError,
    LockTimeoutError,
    RequeueStageError,
    UnknownClientError,
)
from cw.models import (
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._review_debt_helpers import _debt_record

if TYPE_CHECKING:
    from collections.abc import Callable

_CLIENT = "acme"
_PARENT = "3045"
_DEBT_LANE = "debt"
_REPO = "owner/repo"
_PR_NUMBER = 77
_PR_URL = f"https://github.com/{_REPO}/pull/{_PR_NUMBER}"
_OTHER_PR_URL = f"https://github.com/{_REPO}/pull/78"
_CRASH_MSG = "crash before cursor"
_DISK_MSG = "disk gone"


def _write_config(*, toggle: bool = True) -> None:
    """Two declared lanes; ``acme`` carries the toggle unless *toggle* is False."""
    lanes: list[dict[str, object]] = [{"name": "default"}, {"name": _DEBT_LANE}]
    extra: dict[str, object] = (
        {"review": {"debt_autoenqueue": {"lane": _DEBT_LANE}}} if toggle else {}
    )
    write_clients_yaml(
        ClientSpec(name=_CLIENT, workspace_path=".", lanes=lanes, extra=extra),
    )


def _parent_row(
    *,
    pr_url: str | None = _PR_URL,
    lane: str = "default",
    debt: list[Any] | None = None,
) -> TicketTask:
    return TicketTask(
        ticket_id=_PARENT,
        client=_CLIENT,
        lane=lane,
        stage=Stage.IMPL,
        pr_url=pr_url,
        review_debt=debt if debt is not None else [],
        ever_spawned=True,
    )


def _seed(*rows: TicketTask) -> None:
    store = load_dev_queue()
    store.tasks.extend(rows)
    save_dev_queue(store)


def _prime() -> None:
    """Start the consumer with one unrelated merge already in the inbox.

    Forward-only: the first run starts at the inbox end, so the priming merge is
    history and is never back-filled. Tests start from a non-empty inbox.
    """
    record_event(
        OrchestratorEventType.PR_MERGED,
        {"repo": _REPO, "pr_number": 1, "ticket_id": "history", "client": _CLIENT},
    )
    assert enqueue_review_debt_for_merged_prs() == []


def _merge(*, pr_number: int = _PR_NUMBER) -> None:
    record_event(
        OrchestratorEventType.PR_MERGED,
        {
            "repo": _REPO,
            "pr_number": pr_number,
            "ticket_id": _PARENT,
            "client": _CLIENT,
        },
    )


def _payloads(event_type: OrchestratorEventType) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in read_events(event_types=[event_type])]


def _debt_rows() -> list[TicketTask]:
    """Every queued row except the parent (the parent may sit in the debt lane)."""
    return [t for t in load_dev_queue().tasks if t.ticket_id != _PARENT]


def test_merge_enqueues_pending_debt_row_in_debt_lane() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101", key="k1")]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == ["101"]
    [row] = _debt_rows()
    assert row.ticket_id == "101"
    assert row.client == _CLIENT
    assert row.status is QueueItemStatus.PENDING
    assert row.stage is Stage.PLAN
    assert row.priority == DEBT_AUTOENQUEUE_PRIORITY
    assert row.ever_spawned is False


def test_merge_emits_ticket_enqueued_and_debt_enqueued() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101", key="k1")]))
    _prime()
    _merge()
    enqueue_review_debt_for_merged_prs()
    [ticket] = _payloads(OrchestratorEventType.TICKET_ENQUEUED)
    assert ticket == {
        "ticket_id": "101",
        "client": _CLIENT,
        "priority": 0,
        "stage": "plan",
    }
    [enq] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUED)
    assert enq == {
        "client": _CLIENT,
        "ticket_id": _PARENT,
        "debt_ticket_id": "101",
        "lane": _DEBT_LANE,
        "key": "k1",
        "pr_number": _PR_NUMBER,
    }


def test_no_merge_event_changes_nothing() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    _prime()
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []


def test_open_pr_merge_for_other_number_has_no_parent() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    _prime()
    _merge(pr_number=999)
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []


def test_merge_without_debt_records_emits_nothing() -> None:
    _write_config()
    _seed(_parent_row(debt=[]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []
    assert _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED) == []


def test_toggle_absent_advances_cursor_and_changes_nothing() -> None:
    _write_config(toggle=False)
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []
    assert _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED) == []


def test_one_level_skips_when_parent_already_in_debt_lane() -> None:
    _write_config()
    _seed(_parent_row(lane=_DEBT_LANE, debt=[_debt_record(ticket_id="101")]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []
    [skip] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)
    assert skip["reason"] == "one_level"
    assert skip["debt_ticket_id"] == "101"


@pytest.mark.parametrize(
    ("status", "stage"),
    [
        (QueueItemStatus.PENDING, Stage.PLAN),
        (QueueItemStatus.FAILED, Stage.PLAN),
        (QueueItemStatus.COMPLETED, Stage.IMPL),
    ],
    ids=["pending", "failed", "completed-other-stage"],
)
def test_existing_row_at_any_status_or_stage_is_row_exists(
    status: QueueItemStatus, stage: Stage
) -> None:
    _write_config()
    existing = TicketTask(
        ticket_id="101", client=_CLIENT, lane=_DEBT_LANE, status=status, stage=stage
    )
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]), existing)
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    assert len([t for t in load_dev_queue().tasks if t.ticket_id == "101"]) == 1
    [skip] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)
    assert skip["reason"] == "row_exists"


def test_records_without_an_id_are_skipped_silently() -> None:
    _write_config()
    _seed(
        _parent_row(
            debt=[
                _debt_record(ticket_id=None, key="k0", mode="emitted"),
                _debt_record(ticket_id="", key="k1", mode="emitted"),
            ]
        )
    )
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []
    assert _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED) == []


def test_duplicate_debt_ids_in_one_event_enqueue_once() -> None:
    _write_config()
    _seed(
        _parent_row(
            debt=[
                _debt_record(ticket_id="101", key="k1"),
                _debt_record(ticket_id="101", key="k2"),
            ]
        )
    )
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == ["101"]
    assert len(_debt_rows()) == 1


def test_record_with_other_pr_url_is_pr_mismatch() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101", pr_url=_OTHER_PR_URL)]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    [skip] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)
    assert skip["reason"] == "pr_mismatch"


def test_record_with_matching_pr_url_enqueues() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101", pr_url=_PR_URL)]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == ["101"]


def test_unparsable_record_pr_url_is_pr_mismatch() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101", pr_url="not-a-url")]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    [skip] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)
    assert skip["reason"] == "pr_mismatch"


def test_duplicate_parent_rows_pick_the_row_whose_pr_matches() -> None:
    _write_config()
    _seed(
        _parent_row(pr_url=_OTHER_PR_URL, debt=[_debt_record(ticket_id="900")]),
        _parent_row(pr_url=_PR_URL, debt=[_debt_record(ticket_id="101")]),
    )
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == ["101"]


def test_no_matching_parent_row_emits_no_parent_row_skip() -> None:
    _write_config()
    _seed(_parent_row(pr_url=_OTHER_PR_URL, debt=[_debt_record(ticket_id="101")]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    [skip] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)
    assert skip["reason"] == "no_parent_row"
    assert skip["debt_ticket_id"] is None
    assert skip["key"] is None


def test_malformed_payload_is_consumed_without_error() -> None:
    _write_config()
    _prime()
    record_event(OrchestratorEventType.PR_MERGED, {"repo": 5})
    assert enqueue_review_debt_for_merged_prs() == []
    assert enqueue_review_debt_for_merged_prs() == []


def test_first_run_does_not_backfill_a_merge_already_in_the_inbox() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    _merge()  # recorded before the consumer ever ran
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []


def test_empty_first_run_processes_a_later_merge() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    assert enqueue_review_debt_for_merged_prs() == []
    _merge()
    assert enqueue_review_debt_for_merged_prs() == ["101"]
    assert [row.ticket_id for row in _debt_rows()] == ["101"]


def test_invalid_debt_id_is_logged_and_dropped_without_event() -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="bad id!")]))
    _prime()
    _merge()
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []
    assert _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED) == []
    # Cursor advanced past the poison event: the next run is a no-op, not a retry.
    assert enqueue_review_debt_for_merged_prs() == []


@pytest.mark.parametrize(
    "error",
    [
        LaneNotFoundError("lane gone"),
        RequeueStageError("bad stage"),
        UnknownClientError("no client", name="no-client", available=[]),
    ],
    ids=["lane-not-found", "requeue-stage", "unknown-client"],
)
def test_poison_errors_become_lane_undeclared_and_cursor_advances(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    _prime()
    _merge()

    def refuse(_task: TicketTask, **_kwargs: Any) -> StrictAddResult:
        raise error

    monkeypatch.setattr(mod, "add_ticket_if_absent", refuse)
    assert enqueue_review_debt_for_merged_prs() == []
    [skip] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)
    assert skip["reason"] == "lane_undeclared"
    # Cursor advanced: a second run does not re-emit the skip.
    assert enqueue_review_debt_for_merged_prs() == []
    assert len(_payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)) == 1


def test_lock_timeout_keeps_cursor_and_next_run_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    _prime()
    _merge()

    def busy(_task: TicketTask, **_kwargs: Any) -> StrictAddResult:
        msg = "busy"
        raise LockTimeoutError(msg, lock_name="dev_queue", lock_path="x", waited_s=0.0)

    with (
        patch.object(mod, "add_ticket_if_absent", busy),
        pytest.raises(LockTimeoutError),
    ):
        enqueue_review_debt_for_merged_prs()
    assert enqueue_review_debt_for_merged_prs() == ["101"]


def test_partial_batch_emits_inserted_candidate_before_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config()
    _seed(
        _parent_row(
            debt=[
                _debt_record(ticket_id="101", key="k1"),
                _debt_record(ticket_id="102", key="k2"),
            ]
        )
    )
    _prime()
    _merge()

    def first_inserts_second_fails(task: TicketTask, **kwargs: Any) -> StrictAddResult:
        if task.ticket_id == "102":
            raise OSError(_DISK_MSG)
        return add_ticket_if_absent(task, **kwargs)

    with (
        patch.object(mod, "add_ticket_if_absent", first_inserts_second_fails),
        pytest.raises(OSError, match="disk gone"),
    ):
        enqueue_review_debt_for_merged_prs()
    enqueued = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUED)
    assert [e["debt_ticket_id"] for e in enqueued] == ["101"]
    assert enqueue_review_debt_for_merged_prs() == ["102"]
    [skip] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)
    assert skip["debt_ticket_id"] == "101"
    assert skip["reason"] == "row_exists"


def test_crash_before_cursor_replays_without_minting_a_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    _prime()
    _merge()

    def crash(_consumer: str, _event_id: str) -> None:
        raise OSError(_CRASH_MSG)

    with (
        patch.object(mod, "advance_cursor", crash),
        pytest.raises(OSError, match="crash before cursor"),
    ):
        enqueue_review_debt_for_merged_prs()
    assert enqueue_review_debt_for_merged_prs() == []
    assert len(_debt_rows()) == 1
    [skip] = _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED)
    assert skip["reason"] == "row_exists"


def test_adds_hold_only_the_dev_queue_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config()
    _seed(_parent_row(debt=[_debt_record(ticket_id="101")]))
    _prime()
    _merge()
    seen: list[tuple[str, ...]] = []

    def spy_save(store: DevQueueStore) -> None:
        seen.append(tuple(lock.name for lock in _lock_guard.held_locks()))
        save_dev_queue(store)

    monkeypatch.setattr("cw.dev_queue.crud.save_dev_queue", spy_save)
    enqueue_review_debt_for_merged_prs()
    assert seen == [(DEV_QUEUE_LOCK_NAME,)]


def test_insert_audit_is_under_dev_queue_lock_and_skip_audit_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config()
    _seed(
        _parent_row(
            debt=[
                _debt_record(ticket_id="101", key="k1"),
                _debt_record(ticket_id="102", key="k2", pr_url=_OTHER_PR_URL),
            ]
        )
    )
    _prime()
    _merge()
    held_by_type: dict[str, list[tuple[str, ...]]] = {}
    real_record = mod.record_event
    real_crud_record = crud_mod.record_event

    def spy_for(
        real: Callable[..., object],
    ) -> Callable[..., object]:
        def spy(event_type: OrchestratorEventType, *args: Any, **kwargs: Any) -> object:
            held = tuple(lock.name for lock in _lock_guard.held_locks())
            held_by_type.setdefault(event_type.value, []).append(held)
            return real(event_type, *args, **kwargs)

        return spy

    monkeypatch.setattr(mod, "record_event", spy_for(real_record))
    monkeypatch.setattr(crud_mod, "record_event", spy_for(real_crud_record))
    enqueue_review_debt_for_merged_prs()
    assert held_by_type["review.debt_enqueued"] == [(DEV_QUEUE_LOCK_NAME,)]
    assert held_by_type["ticket.enqueued"] == [(DEV_QUEUE_LOCK_NAME,)]
    assert held_by_type["review.debt_enqueue_skipped"] == [()]


def test_merge_for_unknown_client_is_consumed_without_change() -> None:
    _write_config()
    _prime()
    record_event(
        OrchestratorEventType.PR_MERGED,
        {
            "repo": _REPO,
            "pr_number": _PR_NUMBER,
            "ticket_id": _PARENT,
            "client": "ghost",
        },
    )
    assert enqueue_review_debt_for_merged_prs() == []
    assert _debt_rows() == []
    assert _payloads(OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED) == []
    assert enqueue_review_debt_for_merged_prs() == []


def test_original_retryable_error_wins_over_a_failed_partial_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config()
    _seed(
        _parent_row(
            debt=[
                _debt_record(ticket_id="101", key="k1", pr_url=_OTHER_PR_URL),
                _debt_record(ticket_id="102", key="k2"),
            ]
        )
    )
    _prime()
    _merge()

    def first_inserts_second_fails(task: TicketTask, **kwargs: Any) -> StrictAddResult:
        if task.ticket_id == "102":
            raise OSError(_DISK_MSG)
        return add_ticket_if_absent(task, **kwargs)

    def emit_breaks(
        event_type: OrchestratorEventType, *_args: Any, **_kwargs: Any
    ) -> None:
        if event_type is OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED:
            msg = "emit broke"
            raise OSError(msg)

    with (
        patch.object(mod, "add_ticket_if_absent", first_inserts_second_fails),
        patch.object(mod, "record_event", emit_breaks),
        pytest.raises(OSError, match=_DISK_MSG),
    ):
        enqueue_review_debt_for_merged_prs()
    # The cursor stayed put, so the event is still pending for the next run.
    assert _debt_rows() == []
