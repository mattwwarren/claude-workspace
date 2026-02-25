"""Tests for cw.queue - task queue CRUD and state management."""

from __future__ import annotations

import json
import re
from datetime import UTC
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from cw.models import QueueItem, QueueItemStatus, QueueStore, SessionPurpose, TaskSpec
from cw.queue import (
    add_item,
    claim_by_id,
    claim_next,
    clear_queue,
    complete_item,
    fail_item,
    load_queue,
    peek_next,
    remove_item,
    save_queue,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    description: str = "Do the thing",
    purpose: SessionPurpose = SessionPurpose.IMPL,
    prompt: str = "Please do the thing.",
) -> TaskSpec:
    return TaskSpec(description=description, purpose=purpose, prompt=prompt)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_queues_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cw.config.QUEUES_DIR to a temp directory for isolation."""
    queues_dir = tmp_path / "queues"
    queues_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.QUEUES_DIR", queues_dir)
    monkeypatch.setattr("cw.queue.QUEUES_DIR", queues_dir)
    return queues_dir


# ---------------------------------------------------------------------------
# TestLoadSaveQueue
# ---------------------------------------------------------------------------


class TestLoadSaveQueue:
    def test_load_missing_file_returns_empty_store(self, tmp_queues_dir: Path) -> None:
        store = load_queue("test-client")
        assert store.items == []

    def test_save_creates_json_file(self, tmp_queues_dir: Path) -> None:
        store = QueueStore()
        save_queue("test-client", store)
        queue_file = tmp_queues_dir / "test-client.json"
        assert queue_file.exists()

    def test_save_creates_parent_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = tmp_path / "deep" / "queues"
        monkeypatch.setattr("cw.config.QUEUES_DIR", nested)
        monkeypatch.setattr("cw.queue.QUEUES_DIR", nested)
        store = QueueStore()
        save_queue("test-client", store)
        assert (nested / "test-client.json").exists()

    def test_roundtrip_preserves_items(self, tmp_queues_dir: Path) -> None:
        item = QueueItem(client="test-client", task=_make_task())
        store = QueueStore(items=[item])
        save_queue("test-client", store)
        loaded = load_queue("test-client")
        assert len(loaded.items) == 1
        assert loaded.items[0].id == item.id
        assert loaded.items[0].status == QueueItemStatus.PENDING

    def test_roundtrip_preserves_all_statuses(self, tmp_queues_dir: Path) -> None:
        items = [
            QueueItem(
                client="c",
                task=_make_task(description="pending"),
                status=QueueItemStatus.PENDING,
            ),
            QueueItem(
                client="c",
                task=_make_task(description="running"),
                status=QueueItemStatus.RUNNING,
            ),
            QueueItem(
                client="c",
                task=_make_task(description="done"),
                status=QueueItemStatus.COMPLETED,
                result="ok",
            ),
        ]
        store = QueueStore(items=items)
        save_queue("c", store)
        loaded = load_queue("c")
        statuses = {i.status for i in loaded.items}
        assert statuses == {
            QueueItemStatus.PENDING,
            QueueItemStatus.RUNNING,
            QueueItemStatus.COMPLETED,
        }

    def test_file_is_valid_json(self, tmp_queues_dir: Path) -> None:
        store = QueueStore()
        save_queue("test-client", store)
        raw = (tmp_queues_dir / "test-client.json").read_text()
        parsed = json.loads(raw)
        assert "items" in parsed

    def test_separate_clients_have_separate_files(self, tmp_queues_dir: Path) -> None:
        save_queue("client-a", QueueStore())
        save_queue("client-b", QueueStore())
        assert (tmp_queues_dir / "client-a.json").exists()
        assert (tmp_queues_dir / "client-b.json").exists()


# ---------------------------------------------------------------------------
# TestAddItem
# ---------------------------------------------------------------------------


class TestAddItem:
    def test_add_item_returns_queue_item(self, tmp_queues_dir: Path) -> None:
        task = _make_task()
        item = add_item("test-client", task)
        assert isinstance(item, QueueItem)

    def test_add_item_status_is_pending(self, tmp_queues_dir: Path) -> None:
        item = add_item("test-client", _make_task())
        assert item.status == QueueItemStatus.PENDING

    def test_add_item_client_matches(self, tmp_queues_dir: Path) -> None:
        item = add_item("test-client", _make_task())
        assert item.client == "test-client"

    def test_add_item_task_preserved(self, tmp_queues_dir: Path) -> None:
        task = _make_task(description="special task", prompt="do special things")
        item = add_item("test-client", task)
        assert item.task.description == "special task"
        assert item.task.prompt == "do special things"

    def test_add_item_persisted_to_disk(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task())
        reloaded = load_queue("test-client")
        assert len(reloaded.items) == 1

    def test_add_multiple_items_all_persisted(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task(description="first"))
        add_item("test-client", _make_task(description="second"))
        add_item("test-client", _make_task(description="third"))
        store = load_queue("test-client")
        assert len(store.items) == 3

    def test_add_item_has_unique_id(self, tmp_queues_dir: Path) -> None:
        item_a = add_item("test-client", _make_task())
        item_b = add_item("test-client", _make_task())
        assert item_a.id != item_b.id

    def test_add_item_started_at_is_none(self, tmp_queues_dir: Path) -> None:
        item = add_item("test-client", _make_task())
        assert item.started_at is None

    def test_add_item_result_is_none(self, tmp_queues_dir: Path) -> None:
        item = add_item("test-client", _make_task())
        assert item.result is None


# ---------------------------------------------------------------------------
# TestClaimNext
# ---------------------------------------------------------------------------


class TestClaimNext:
    def test_claim_next_empty_queue_returns_none(self, tmp_queues_dir: Path) -> None:
        result = claim_next("test-client")
        assert result is None

    def test_claim_next_returns_pending_item(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task())
        item = claim_next("test-client")
        assert item is not None

    def test_claim_next_marks_as_running(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task())
        item = claim_next("test-client")
        assert item is not None
        assert item.status == QueueItemStatus.RUNNING

    def test_claim_next_sets_started_at(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task())
        item = claim_next("test-client")
        assert item is not None
        assert item.started_at is not None

    def test_claim_next_persists_running_status(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task())
        claimed = claim_next("test-client")
        assert claimed is not None
        store = load_queue("test-client")
        persisted = store.find_item(claimed.id)
        assert persisted is not None
        assert persisted.status == QueueItemStatus.RUNNING

    def test_claim_next_returns_oldest_pending(self, tmp_queues_dir: Path) -> None:
        first = add_item("test-client", _make_task(description="first"))
        add_item("test-client", _make_task(description="second"))
        claimed = claim_next("test-client")
        assert claimed is not None
        assert claimed.id == first.id

    def test_claim_next_skips_running_items(self, tmp_queues_dir: Path) -> None:
        first = add_item("test-client", _make_task(description="first"))
        second = add_item("test-client", _make_task(description="second"))

        # Manually set first to running so claim should return second
        store = load_queue("test-client")
        store.find_item(first.id).status = QueueItemStatus.RUNNING  # type: ignore[union-attr]
        save_queue("test-client", store)

        claimed = claim_next("test-client")
        assert claimed is not None
        assert claimed.id == second.id

    def test_double_claim_returns_different_item(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task(description="a"))
        add_item("test-client", _make_task(description="b"))
        first_claim = claim_next("test-client")
        second_claim = claim_next("test-client")
        assert first_claim is not None
        assert second_claim is not None
        assert first_claim.id != second_claim.id

    def test_double_claim_exhausts_single_item(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task())
        first_claim = claim_next("test-client")
        second_claim = claim_next("test-client")
        assert first_claim is not None
        assert second_claim is None

    def test_claim_next_skips_completed_items(self, tmp_queues_dir: Path) -> None:
        item = add_item("test-client", _make_task())
        store = load_queue("test-client")
        store.find_item(item.id).status = QueueItemStatus.COMPLETED  # type: ignore[union-attr]
        save_queue("test-client", store)
        result = claim_next("test-client")
        assert result is None

    def test_claim_next_skips_failed_items(self, tmp_queues_dir: Path) -> None:
        item = add_item("test-client", _make_task())
        store = load_queue("test-client")
        store.find_item(item.id).status = QueueItemStatus.FAILED  # type: ignore[union-attr]
        save_queue("test-client", store)
        result = claim_next("test-client")
        assert result is None


# ---------------------------------------------------------------------------
# TestClaimNextWithPurposeFilter
# ---------------------------------------------------------------------------


class TestClaimNextWithPurposeFilter:
    def test_purpose_filter_returns_matching_item(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IMPL))
        claimed = claim_next("c", purpose=SessionPurpose.IMPL)
        assert claimed is not None
        assert claimed.task.purpose == SessionPurpose.IMPL

    def test_purpose_filter_skips_non_matching(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        result = claim_next("c", purpose=SessionPurpose.IMPL)
        assert result is None

    def test_purpose_filter_none_claims_any(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        result = claim_next("c", purpose=None)
        assert result is not None

    def test_purpose_filter_selects_correct_among_mixed(
        self, tmp_queues_dir: Path
    ) -> None:
        idea_task = _make_task(
            description="idea task",
            purpose=SessionPurpose.IDEA,
        )
        add_item("c", idea_task)
        impl_item = add_item(
            "c", _make_task(description="impl task", purpose=SessionPurpose.IMPL)
        )
        claimed = claim_next("c", purpose=SessionPurpose.IMPL)
        assert claimed is not None
        assert claimed.id == impl_item.id

    def test_purpose_filter_skips_earlier_wrong_purpose(
        self, tmp_queues_dir: Path
    ) -> None:
        # Add idea first so it would be the "oldest"
        add_item("c", _make_task(description="idea", purpose=SessionPurpose.IDEA))
        impl = add_item(
            "c", _make_task(description="impl", purpose=SessionPurpose.IMPL)
        )
        claimed = claim_next("c", purpose=SessionPurpose.IMPL)
        assert claimed is not None
        assert claimed.id == impl.id


# ---------------------------------------------------------------------------
# TestCompleteItem
# ---------------------------------------------------------------------------


class TestCompleteItem:
    def test_complete_sets_completed_status(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        complete_item("c", item.id, "all done")
        store = load_queue("c")
        persisted = store.find_item(item.id)
        assert persisted is not None
        assert persisted.status == QueueItemStatus.COMPLETED

    def test_complete_stores_result(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        complete_item("c", item.id, "my result string")
        store = load_queue("c")
        persisted = store.find_item(item.id)
        assert persisted is not None
        assert persisted.result == "my result string"

    def test_complete_sets_completed_at(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        complete_item("c", item.id, "done")
        store = load_queue("c")
        persisted = store.find_item(item.id)
        assert persisted is not None
        assert persisted.completed_at is not None

    def test_complete_item_not_found_raises(self, tmp_queues_dir: Path) -> None:
        with pytest.raises(ValueError, match="Queue item not found"):
            complete_item("c", "nonexistent-id", "result")

    def test_complete_persists_to_disk(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        complete_item("c", item.id, "persisted result")
        reloaded = load_queue("c")
        persisted = reloaded.find_item(item.id)
        assert persisted is not None
        assert persisted.status == QueueItemStatus.COMPLETED
        assert persisted.result == "persisted result"


# ---------------------------------------------------------------------------
# TestFailItem
# ---------------------------------------------------------------------------


class TestFailItem:
    def test_fail_sets_failed_status(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        fail_item("c", item.id, "something went wrong")
        store = load_queue("c")
        persisted = store.find_item(item.id)
        assert persisted is not None
        assert persisted.status == QueueItemStatus.FAILED

    def test_fail_stores_error_in_result(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        fail_item("c", item.id, "timeout error")
        store = load_queue("c")
        persisted = store.find_item(item.id)
        assert persisted is not None
        assert persisted.result == "timeout error"

    def test_fail_sets_completed_at(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        fail_item("c", item.id, "error")
        store = load_queue("c")
        persisted = store.find_item(item.id)
        assert persisted is not None
        assert persisted.completed_at is not None

    def test_fail_item_not_found_raises(self, tmp_queues_dir: Path) -> None:
        with pytest.raises(ValueError, match="Queue item not found"):
            fail_item("c", "bad-id", "error")

    def test_fail_persists_to_disk(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        fail_item("c", item.id, "crash")
        reloaded = load_queue("c")
        persisted = reloaded.find_item(item.id)
        assert persisted is not None
        assert persisted.status == QueueItemStatus.FAILED


# ---------------------------------------------------------------------------
# TestRemoveItem
# ---------------------------------------------------------------------------


class TestRemoveItem:
    def test_remove_deletes_item(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        remove_item("c", item.id)
        store = load_queue("c")
        assert store.find_item(item.id) is None

    def test_remove_persists_deletion(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        remove_item("c", item.id)
        reloaded = load_queue("c")
        assert len(reloaded.items) == 0

    def test_remove_leaves_other_items(self, tmp_queues_dir: Path) -> None:
        item_a = add_item("c", _make_task(description="a"))
        item_b = add_item("c", _make_task(description="b"))
        remove_item("c", item_a.id)
        store = load_queue("c")
        assert store.find_item(item_a.id) is None
        assert store.find_item(item_b.id) is not None

    def test_remove_nonexistent_id_is_no_op(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task())
        remove_item("c", "nonexistent-id")
        store = load_queue("c")
        assert len(store.items) == 1

    def test_remove_from_empty_queue_is_no_op(self, tmp_queues_dir: Path) -> None:
        remove_item("c", "any-id")
        store = load_queue("c")
        assert store.items == []


# ---------------------------------------------------------------------------
# TestClearQueue
# ---------------------------------------------------------------------------


class TestClearQueue:
    def test_clear_all_no_filters(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IMPL))
        add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        removed = clear_queue("c")
        assert removed == 2
        assert load_queue("c").items == []

    def test_clear_returns_count_removed(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task())
        add_item("c", _make_task())
        add_item("c", _make_task())
        removed = clear_queue("c")
        assert removed == 3

    def test_clear_empty_queue_returns_zero(self, tmp_queues_dir: Path) -> None:
        removed = clear_queue("c")
        assert removed == 0

    def test_clear_with_purpose_filter(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IMPL))
        add_item("c", _make_task(purpose=SessionPurpose.IMPL))
        idea_item = add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        removed = clear_queue("c", purpose=SessionPurpose.IMPL)
        assert removed == 2
        store = load_queue("c")
        assert len(store.items) == 1
        assert store.items[0].id == idea_item.id

    def test_clear_with_status_filter(self, tmp_queues_dir: Path) -> None:
        pending_item = add_item("c", _make_task())
        running_item = add_item("c", _make_task())
        # Manually set second item to RUNNING
        store = load_queue("c")
        store.find_item(running_item.id).status = QueueItemStatus.RUNNING  # type: ignore[union-attr]
        save_queue("c", store)

        removed = clear_queue("c", status=QueueItemStatus.RUNNING)
        assert removed == 1
        reloaded = load_queue("c")
        assert len(reloaded.items) == 1
        assert reloaded.items[0].id == pending_item.id

    def test_clear_with_both_filters(self, tmp_queues_dir: Path) -> None:
        impl_pending = add_item("c", _make_task(purpose=SessionPurpose.IMPL))
        idea_pending = add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        impl_running = add_item("c", _make_task(purpose=SessionPurpose.IMPL))
        # Set third to running
        store = load_queue("c")
        store.find_item(impl_running.id).status = QueueItemStatus.RUNNING  # type: ignore[union-attr]
        save_queue("c", store)

        # Only remove IMPL + PENDING
        removed = clear_queue(
            "c", purpose=SessionPurpose.IMPL, status=QueueItemStatus.PENDING
        )
        assert removed == 1
        reloaded = load_queue("c")
        remaining_ids = {i.id for i in reloaded.items}
        assert impl_pending.id not in remaining_ids
        assert idea_pending.id in remaining_ids
        assert impl_running.id in remaining_ids

    def test_clear_purpose_filter_no_match_returns_zero(
        self, tmp_queues_dir: Path
    ) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        removed = clear_queue("c", purpose=SessionPurpose.IMPL)
        assert removed == 0
        assert len(load_queue("c").items) == 1

    def test_clear_persists_remaining_items(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IMPL))
        keep = add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        clear_queue("c", purpose=SessionPurpose.IMPL)
        reloaded = load_queue("c")
        assert len(reloaded.items) == 1
        assert reloaded.items[0].id == keep.id


# ---------------------------------------------------------------------------
# TestQueueStoreMethods
# ---------------------------------------------------------------------------


class TestQueueStoreMethods:
    def _store_with_items(self) -> QueueStore:
        impl_pending = QueueItem(
            client="c",
            task=_make_task(description="impl pending", purpose=SessionPurpose.IMPL),
            status=QueueItemStatus.PENDING,
        )
        impl_running = QueueItem(
            client="c",
            task=_make_task(description="impl running", purpose=SessionPurpose.IMPL),
            status=QueueItemStatus.RUNNING,
        )
        idea_pending = QueueItem(
            client="c",
            task=_make_task(description="idea pending", purpose=SessionPurpose.IDEA),
            status=QueueItemStatus.PENDING,
        )
        failed = QueueItem(
            client="c",
            task=_make_task(description="failed", purpose=SessionPurpose.DEBT),
            status=QueueItemStatus.FAILED,
        )
        return QueueStore(items=[impl_pending, impl_running, idea_pending, failed])

    def test_pending_returns_only_pending(self) -> None:
        store = self._store_with_items()
        pending = store.pending()
        assert len(pending) == 2
        assert all(i.status == QueueItemStatus.PENDING for i in pending)

    def test_pending_empty_when_none(self) -> None:
        store = QueueStore()
        assert store.pending() == []

    def test_running_returns_only_running(self) -> None:
        store = self._store_with_items()
        running = store.running()
        assert len(running) == 1
        assert running[0].status == QueueItemStatus.RUNNING

    def test_running_empty_when_none(self) -> None:
        store = QueueStore()
        assert store.running() == []

    def test_by_purpose_filters_correctly(self) -> None:
        store = self._store_with_items()
        impl_items = store.by_purpose(SessionPurpose.IMPL)
        assert len(impl_items) == 2
        assert all(i.task.purpose == SessionPurpose.IMPL for i in impl_items)

    def test_by_purpose_returns_empty_for_unknown(self) -> None:
        store = self._store_with_items()
        result = store.by_purpose(SessionPurpose.EXPLORE)
        assert result == []

    def test_by_status_filters_correctly(self) -> None:
        store = self._store_with_items()
        failed_items = store.by_status(QueueItemStatus.FAILED)
        assert len(failed_items) == 1
        assert failed_items[0].status == QueueItemStatus.FAILED

    def test_by_status_returns_empty_for_no_match(self) -> None:
        store = self._store_with_items()
        completed = store.by_status(QueueItemStatus.COMPLETED)
        assert completed == []

    def test_find_item_returns_correct_item(self) -> None:
        store = self._store_with_items()
        target = store.items[2]
        found = store.find_item(target.id)
        assert found is not None
        assert found.id == target.id

    def test_find_item_returns_none_on_miss(self) -> None:
        store = self._store_with_items()
        assert store.find_item("nonexistent") is None

    def test_find_item_returns_none_on_empty_store(self) -> None:
        store = QueueStore()
        assert store.find_item("any-id") is None


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_operations_on_different_clients_are_isolated(
        self, tmp_queues_dir: Path
    ) -> None:
        item_a = add_item("client-a", _make_task(description="task a"))
        add_item("client-b", _make_task(description="task b"))

        store_a = load_queue("client-a")
        store_b = load_queue("client-b")

        assert len(store_a.items) == 1
        assert len(store_b.items) == 1
        assert store_a.items[0].id == item_a.id

    def test_complete_item_error_message_contains_id(
        self, tmp_queues_dir: Path
    ) -> None:
        with pytest.raises(ValueError, match="bad-id-123"):
            complete_item("c", "bad-id-123", "result")

    def test_fail_item_error_message_contains_id(self, tmp_queues_dir: Path) -> None:
        with pytest.raises(ValueError, match="missing-id"):
            fail_item("c", "missing-id", "error")

    def test_claim_next_on_all_completed_returns_none(
        self, tmp_queues_dir: Path
    ) -> None:
        item = add_item("c", _make_task())
        store = load_queue("c")
        store.find_item(item.id).status = QueueItemStatus.COMPLETED  # type: ignore[union-attr]
        store.find_item(item.id).result = "done"  # type: ignore[union-attr]
        save_queue("c", store)
        assert claim_next("c") is None

    def test_add_item_with_optional_task_fields(self, tmp_queues_dir: Path) -> None:
        task = TaskSpec(
            description="full task",
            purpose=SessionPurpose.IDEA,
            prompt="review everything",
            context_files=["src/main.py", "tests/test_main.py"],
            success_criteria="No regressions",
            source_session="sess-abc",
        )
        item = add_item("c", task)
        store = load_queue("c")
        persisted = store.find_item(item.id)
        assert persisted is not None
        assert persisted.task.context_files == ["src/main.py", "tests/test_main.py"]
        assert persisted.task.success_criteria == "No regressions"
        assert persisted.task.source_session == "sess-abc"

    def test_queue_item_id_is_8_char_hex(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        assert re.match(r"^[0-9a-f]{8}$", item.id)

    def test_created_at_has_utc_timezone(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        assert item.created_at.tzinfo is not None
        assert item.created_at.tzinfo == UTC


# ---------------------------------------------------------------------------
# TestPrioritySorting
# ---------------------------------------------------------------------------


class TestPrioritySorting:
    def test_higher_priority_claimed_first(self, tmp_queues_dir: Path) -> None:
        add_item(
            "c",
            _make_task(description="low", purpose=SessionPurpose.DEBT),
        )
        high = add_item(
            "c",
            TaskSpec(
                description="high",
                purpose=SessionPurpose.DEBT,
                prompt="high priority",
                priority=10,
            ),
        )
        claimed = claim_next("c")
        assert claimed is not None
        assert claimed.id == high.id

    def test_fifo_within_same_priority(self, tmp_queues_dir: Path) -> None:
        first = add_item("c", _make_task(description="first"))
        add_item("c", _make_task(description="second"))
        claimed = claim_next("c")
        assert claimed is not None
        assert claimed.id == first.id

    def test_priority_zero_is_default(self, tmp_queues_dir: Path) -> None:
        item = add_item("c", _make_task())
        assert item.task.priority == 0

    def test_multiple_priority_tiers(self, tmp_queues_dir: Path) -> None:
        low = add_item(
            "c",
            TaskSpec(
                description="low",
                purpose=SessionPurpose.DEBT,
                prompt="low",
                priority=1,
            ),
        )
        mid = add_item(
            "c",
            TaskSpec(
                description="mid",
                purpose=SessionPurpose.DEBT,
                prompt="mid",
                priority=5,
            ),
        )
        high = add_item(
            "c",
            TaskSpec(
                description="high",
                purpose=SessionPurpose.DEBT,
                prompt="high",
                priority=10,
            ),
        )
        first = claim_next("c")
        second = claim_next("c")
        third = claim_next("c")
        assert first is not None
        assert first.id == high.id
        assert second is not None
        assert second.id == mid.id
        assert third is not None
        assert third.id == low.id

    def test_priority_with_purpose_filter(self, tmp_queues_dir: Path) -> None:
        add_item(
            "c",
            TaskSpec(
                description="impl low",
                purpose=SessionPurpose.IMPL,
                prompt="impl low",
                priority=1,
            ),
        )
        high_debt = add_item(
            "c",
            TaskSpec(
                description="debt high",
                purpose=SessionPurpose.DEBT,
                prompt="debt high",
                priority=10,
            ),
        )
        add_item(
            "c",
            TaskSpec(
                description="debt low",
                purpose=SessionPurpose.DEBT,
                prompt="debt low",
                priority=1,
            ),
        )
        claimed = claim_next("c", purpose=SessionPurpose.DEBT)
        assert claimed is not None
        assert claimed.id == high_debt.id

    def test_negative_priority(self, tmp_queues_dir: Path) -> None:
        normal = add_item("c", _make_task(description="normal"))
        add_item(
            "c",
            TaskSpec(
                description="deprioritized",
                purpose=SessionPurpose.IMPL,
                prompt="low",
                priority=-5,
            ),
        )
        claimed = claim_next("c")
        assert claimed is not None
        assert claimed.id == normal.id

    def test_priority_preserved_in_roundtrip(self, tmp_queues_dir: Path) -> None:
        add_item(
            "c",
            TaskSpec(
                description="test",
                purpose=SessionPurpose.DEBT,
                prompt="test",
                priority=42,
            ),
        )
        store = load_queue("c")
        assert store.items[0].task.priority == 42


# ---------------------------------------------------------------------------
# TestPeekNext
# ---------------------------------------------------------------------------


class TestPeekNext:
    def test_peek_empty_queue_returns_none(self, tmp_queues_dir: Path) -> None:
        result = peek_next("test-client")
        assert result is None

    def test_peek_returns_pending_item(self, tmp_queues_dir: Path) -> None:
        add_item("test-client", _make_task())
        item = peek_next("test-client")
        assert item is not None

    def test_peek_does_not_mutate_status(self, tmp_queues_dir: Path) -> None:
        added = add_item("test-client", _make_task())
        peek_next("test-client")
        store = load_queue("test-client")
        persisted = store.find_item(added.id)
        assert persisted is not None
        assert persisted.status == QueueItemStatus.PENDING

    def test_peek_returns_highest_priority(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(description="low"))
        high = add_item(
            "c",
            TaskSpec(
                description="high",
                purpose=SessionPurpose.IMPL,
                prompt="high",
                priority=10,
            ),
        )
        peeked = peek_next("c")
        assert peeked is not None
        assert peeked.id == high.id

    def test_peek_with_purpose_filter(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        impl = add_item("c", _make_task(purpose=SessionPurpose.IMPL))
        peeked = peek_next("c", purpose=SessionPurpose.IMPL)
        assert peeked is not None
        assert peeked.id == impl.id

    def test_peek_with_purpose_filter_no_match(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(purpose=SessionPurpose.IDEA))
        result = peek_next("c", purpose=SessionPurpose.IMPL)
        assert result is None

    def test_peek_is_idempotent(self, tmp_queues_dir: Path) -> None:
        added = add_item("c", _make_task())
        first = peek_next("c")
        second = peek_next("c")
        assert first is not None
        assert second is not None
        assert first.id == added.id
        assert second.id == added.id


# ---------------------------------------------------------------------------
# TestClaimById
# ---------------------------------------------------------------------------


class TestClaimById:
    def test_claim_by_id_returns_item(self, tmp_queues_dir: Path) -> None:
        added = add_item("c", _make_task())
        claimed = claim_by_id("c", added.id)
        assert claimed.id == added.id

    def test_claim_by_id_marks_running(self, tmp_queues_dir: Path) -> None:
        added = add_item("c", _make_task())
        claimed = claim_by_id("c", added.id)
        assert claimed.status == QueueItemStatus.RUNNING

    def test_claim_by_id_sets_started_at(self, tmp_queues_dir: Path) -> None:
        added = add_item("c", _make_task())
        claimed = claim_by_id("c", added.id)
        assert claimed.started_at is not None

    def test_claim_by_id_persists(self, tmp_queues_dir: Path) -> None:
        added = add_item("c", _make_task())
        claim_by_id("c", added.id)
        store = load_queue("c")
        persisted = store.find_item(added.id)
        assert persisted is not None
        assert persisted.status == QueueItemStatus.RUNNING

    def test_claim_by_id_not_found_raises(self, tmp_queues_dir: Path) -> None:
        with pytest.raises(ValueError, match="Queue item not found"):
            claim_by_id("c", "nonexistent")

    def test_claim_by_id_not_pending_raises(self, tmp_queues_dir: Path) -> None:
        added = add_item("c", _make_task())
        claim_by_id("c", added.id)  # Now RUNNING
        with pytest.raises(ValueError, match="not pending"):
            claim_by_id("c", added.id)

    def test_claim_by_id_skips_completed(self, tmp_queues_dir: Path) -> None:
        added = add_item("c", _make_task())
        complete_item("c", added.id, "done")
        with pytest.raises(ValueError, match="not pending"):
            claim_by_id("c", added.id)

    def test_claim_by_id_specific_among_many(self, tmp_queues_dir: Path) -> None:
        add_item("c", _make_task(description="first"))
        second = add_item("c", _make_task(description="second"))
        add_item("c", _make_task(description="third"))
        claimed = claim_by_id("c", second.id)
        assert claimed.id == second.id
        assert claimed.task.description == "second"
