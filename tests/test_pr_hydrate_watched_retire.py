"""Watch hydration cap, terminal skip and retirement (GitHub #3654)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import DevQueueStore, PrState, QueueItemStatus, TicketTask, WatchedPr
from cw.pr_hydrate.transitions import _is_watched_candidate
from cw.pr_hydrate.watched import (
    WATCHED_HYDRATION_CAP_PER_PASS,
    retire_terminal_watched_prs,
    select_watched_hydration_batch,
    sweep_terminal_watched_prs,
)
from tests._shared.session_seeding import _make_ticket_task

_T0 = datetime(2026, 11, 1, tzinfo=UTC)


def _watch(
    pr_number: int = 42,
    *,
    client: str | None = "client-a",
    status: Literal["active", "dismissed"] = "active",
    state: str | None = None,
    hydrated_at: datetime | None = None,
) -> WatchedPr:
    pr_state = None
    if state is not None:
        pr_state = PrState(state=state, hydrated_at=hydrated_at or _T0)
    return WatchedPr(
        pr_url=f"https://github.com/acme/widgets/pull/{pr_number}",
        repo="acme/widgets",
        pr_number=pr_number,
        client=client,
        source="stale_dispatch_park",
        status=status,
        pr_state=pr_state,
    )


def _referencing_row(pr_number: int = 42, *, client: str = "client-a") -> TicketTask:
    return _make_ticket_task(
        ticket_id=f"REF-{pr_number}-{client}",
        client=client,
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=pr_number,
        session_id=None,
    )


class TestIsWatchedCandidate:
    @pytest.mark.parametrize(
        ("status", "state", "expected"),
        [
            ("active", None, True),
            ("active", "OPEN", True),
            ("active", "MERGED", False),
            ("active", "CLOSED", False),
            ("dismissed", None, False),
            ("dismissed", "OPEN", False),
        ],
    )
    def test_candidate_predicate(
        self,
        status: Literal["active", "dismissed"],
        state: str | None,
        expected: bool,
    ) -> None:
        assert _is_watched_candidate(_watch(status=status, state=state)) is expected


class TestSelectWatchedHydrationBatch:
    def test_empty_input(self) -> None:
        assert select_watched_hydration_batch([]) == []

    def test_excludes_dismissed_and_terminal(self) -> None:
        keep = _watch(1, state="OPEN")
        batch = select_watched_hydration_batch(
            [
                _watch(2, status="dismissed", state="OPEN"),
                _watch(3, state="MERGED"),
                _watch(4, state="CLOSED"),
                keep,
            ]
        )
        assert batch == [keep]

    def test_never_hydrated_sorts_before_hydrated(self) -> None:
        old = _watch(1, state="OPEN", hydrated_at=_T0)
        fresh = _watch(2, state=None)
        batch = select_watched_hydration_batch([old, fresh])
        assert batch == [fresh, old]

    def test_oldest_hydrated_first_then_capped(self) -> None:
        watches = [
            _watch(n, state="OPEN", hydrated_at=_T0 + timedelta(seconds=n))
            for n in range(1, WATCHED_HYDRATION_CAP_PER_PASS + 6)
        ]
        batch = select_watched_hydration_batch(list(reversed(watches)))
        assert len(batch) == WATCHED_HYDRATION_CAP_PER_PASS
        assert [w.pr_number for w in batch] == list(
            range(1, WATCHED_HYDRATION_CAP_PER_PASS + 1)
        )


class TestRetireTerminalWatchedPrs:
    def test_unreferenced_terminal_watch_is_dismissed(self) -> None:
        watch = _watch(42, state="MERGED")
        store = DevQueueStore(watched_prs=[watch])

        retired = retire_terminal_watched_prs(store)

        assert retired == [watch]
        assert watch.status == "dismissed"

    def test_closed_unmerged_unreferenced_is_dismissed(self) -> None:
        watch = _watch(42, state="CLOSED")
        retire_terminal_watched_prs(DevQueueStore(watched_prs=[watch]))
        assert watch.status == "dismissed"

    def test_referenced_watch_is_kept(self) -> None:
        watch = _watch(42, state="MERGED")
        store = DevQueueStore(
            tasks=[_referencing_row(42, client="client-a")], watched_prs=[watch]
        )

        assert retire_terminal_watched_prs(store) == []
        assert watch.status == "active"

    def test_other_client_row_does_not_reference(self) -> None:
        watch = _watch(42, client="client-a", state="MERGED")
        store = DevQueueStore(
            tasks=[_referencing_row(42, client="client-b")], watched_prs=[watch]
        )

        retire_terminal_watched_prs(store)

        assert watch.status == "dismissed"

    def test_terminal_status_row_does_not_reference(self) -> None:
        watch = _watch(42, state="MERGED")
        row = _make_ticket_task(
            ticket_id="DONE-42",
            client="client-a",
            status=QueueItemStatus.COMPLETED,
            blocked_on_pr=42,
        )
        retire_terminal_watched_prs(DevQueueStore(tasks=[row], watched_prs=[watch]))
        assert watch.status == "dismissed"

    def test_client_less_terminal_watch_retired_even_with_matching_row(self) -> None:
        watch = _watch(42, client=None, state="MERGED")
        store = DevQueueStore(
            tasks=[_referencing_row(42, client="client-a")], watched_prs=[watch]
        )

        retire_terminal_watched_prs(store)

        assert watch.status == "dismissed"

    def test_open_or_unhydrated_watch_not_retired(self) -> None:
        open_watch = _watch(1, state="OPEN")
        unhydrated = _watch(2, state=None)
        store = DevQueueStore(watched_prs=[open_watch, unhydrated])

        assert retire_terminal_watched_prs(store) == []
        assert open_watch.status == "active"
        assert unhydrated.status == "active"

    def test_already_dismissed_not_returned_and_idempotent(self) -> None:
        watch = _watch(42, state="MERGED")
        store = DevQueueStore(watched_prs=[watch])

        first = retire_terminal_watched_prs(store)
        second = retire_terminal_watched_prs(store)

        assert first == [watch]
        assert second == []
        assert watch.status == "dismissed"


class TestSweepTerminalWatchedPrs:
    def test_sweep_persists_dismissal(self) -> None:
        save_dev_queue(DevQueueStore(watched_prs=[_watch(42, state="MERGED")]))

        sweep_terminal_watched_prs()

        reloaded = load_dev_queue()
        assert [w.status for w in reloaded.watched_prs] == ["dismissed"]

    def test_sweep_noop_does_not_save(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_dev_queue(DevQueueStore(watched_prs=[_watch(1, state="OPEN")]))
        calls: list[DevQueueStore] = []
        monkeypatch.setattr("cw.pr_hydrate.watched.save_dev_queue", calls.append)

        sweep_terminal_watched_prs()

        assert calls == []


class TestRetiredWatchDoesNotCollide:
    def test_retired_watch_does_not_collide_with_other_client(self) -> None:
        from cw.dev_queue.crud import register_or_adopt_watched_pr

        save_dev_queue(DevQueueStore(watched_prs=[_watch(42, state="MERGED")]))
        store = load_dev_queue()
        retire_terminal_watched_prs(store)
        save_dev_queue(store)

        outcome = register_or_adopt_watched_pr(
            _watch(42, client="client-b", state=None)
        )

        assert outcome == "inserted"
        statuses = sorted((w.client, w.status) for w in load_dev_queue().watched_prs)
        assert statuses == [("client-a", "dismissed"), ("client-b", "active")]
