"""Tests for the pre-dispatch closed-ticket guard (#1605).

Resolve half: ``cw.dispatch.ticket_state_gate.resolve_closed_ticket_hits``
(lock-free ``gh issue view`` reads, the per-row cache, the read cap, budget
and backoff, ordering and lane restriction). Claim half:
``_claim_next_pending(closed_ticket_hits=...)`` (cancel, defer hold, identity
re-check, sticky requeue). All gh traffic goes through ``FakeGhCli``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dev_queue.requeue import requeue_ticket
from cw.dispatch import ticket_state_gate as gate
from cw.dispatch.claim.screening import _claim_next_pending, _live_closed_ticket_hit
from cw.dispatch.lanes import _resolve_closed_ticket_hits_if_gated
from cw.dispatch.ticket_state_gate import (
    _FAILED_READ_BACKOFF_SECONDS,
    _MAX_STATE_READS_PER_TICK,
    _OPEN_STATE_TTL_SECONDS,
    _STATE_READ_TIMEOUT_SECONDS,
    TICKET_CLOSED_CANCEL_REASON,
    ClosedTicketHit,
    claimable_lane_names,
    resolve_closed_ticket_hits,
)
from cw.dispatch_state_ticket_state import (
    TicketStateProbe,
    load_ticket_state_probes,
    save_ticket_state_probes,
    ticket_state_key,
)
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.models.orchestrator_config.lane import LaneConfig
from tests._review_debt_helpers import _register_debt_client
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_CLIENT = "acme"
_LANE = "default"
_T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
_CLOSED = '{"state": "CLOSED"}'
_OPEN = '{"state": "OPEN"}'
_LANES = frozenset({_LANE, "fast"})


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def client(root: Path) -> ClientConfig:
    _register_debt_client(root, name=_CLIENT, tracker="github-issues")
    return ClientConfig(name=_CLIENT, workspace_path=root, default_branch="main")


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    return FakeGhCli().install(monkeypatch)


def _row(ticket_id: str = "101", **overrides: object) -> TicketTask:
    fields: dict[str, object] = {
        "ticket_id": ticket_id,
        "client": _CLIENT,
        "stage": Stage.PLAN,
        "lane": _LANE,
        "created_at": _T0,
    }
    fields.update(overrides)
    return _make_ticket_task(**fields)


def _resolve(
    client: ClientConfig, *rows: TicketTask, **kwargs: Any
) -> dict[str, ClosedTicketHit]:
    kwargs.setdefault("claimable_lanes", _LANES)
    kwargs.setdefault("now", _T0)
    return resolve_closed_ticket_hits(client, DevQueueStore(tasks=list(rows)), **kwargs)


def _key(row: TicketTask) -> str:
    return ticket_state_key(row.client, row.ticket_id, row.created_at)


def _read_ids(cli: FakeGhCli) -> list[str]:
    return [record.argv[3] for record in cli.records]


def _staggered(count: int) -> list[TicketTask]:
    return [
        _row(str(100 + i), created_at=_T0 + timedelta(seconds=i)) for i in range(count)
    ]


# --- resolve: verdicts -------------------------------------------------------


def test_closed_issue_is_a_cancel_hit_and_is_cached(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_CLOSED)
    row = _row()

    hits = _resolve(client, row)

    assert hits == {"101": ClosedTicketHit(row.created_at, defer=False)}
    assert load_ticket_state_probes()[_key(row)].closed is True


def test_open_issue_is_no_hit_and_is_cached_with_its_ttl(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    row = _row()

    assert _resolve(client, row) == {}
    probe = load_ticket_state_probes()[_key(row)]
    assert probe == TicketStateProbe(checked_at=_T0, closed=False)
    later = _T0 + timedelta(seconds=_OPEN_STATE_TTL_SECONDS - 1)
    assert _resolve(client, row, now=later) == {}
    assert len(cli.records) == 1
    expired = _T0 + timedelta(seconds=_OPEN_STATE_TTL_SECONDS)
    assert _resolve(client, row, now=expired) == {}
    assert len(cli.records) == 2


_ERRORS: list[dict[str, Any]] = [
    {"returncode": 1, "stdout": ""},
    {"raises": subprocess.TimeoutExpired(["gh"], 5)},
    {"raises": FileNotFoundError("gh")},
    {"stdout": "not json"},
    {"stdout": '{"state": "MERGED"}'},
    {"stdout": '{"state": 3}'},
    {"stdout": "{}"},
]


@pytest.mark.parametrize(
    "reply",
    _ERRORS,
    ids=["nonzero", "timeout", "no-gh", "bad-json", "merged", "non-str", "empty"],
)
def test_a_failed_read_is_never_a_hit_and_records_no_verdict(
    client: ClientConfig, cli: FakeGhCli, reply: dict[str, Any]
) -> None:
    cli.reply("gh", "issue", "view", "200", **reply)
    cli.reply("gh", "issue", "view", "201", stdout=_CLOSED)
    bad, good = _row("200"), _row("201", created_at=_T0 + timedelta(seconds=1))

    hits = _resolve(client, bad, good)

    assert hits == {"201": ClosedTicketHit(good.created_at, defer=False)}
    probe = load_ticket_state_probes()[_key(bad)]
    assert probe.closed is False
    assert probe.checked_at is None
    assert probe.attempted_at == _T0
    # Within the backoff: no re-read, still no hit, the row dispatches.
    assert _resolve(client, bad, now=_T0 + timedelta(seconds=1)) == {}
    assert _read_ids(cli) == ["200", "201"]
    # After the backoff: re-read (never latches).
    retry = _T0 + timedelta(seconds=_FAILED_READ_BACKOFF_SECONDS)
    assert _resolve(client, bad, now=retry) == {}
    assert _read_ids(cli) == ["200", "201", "200"]


def test_a_failed_read_after_an_expired_open_keeps_no_fresh_verdict(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    row = _row()
    old = _T0 - timedelta(seconds=_OPEN_STATE_TTL_SECONDS + 1)
    save_ticket_state_probes(
        _CLIENT,
        {_key(row): TicketStateProbe(checked_at=old, closed=False)},
        prune_to_keys=[_key(row)],
    )
    cli.reply("gh", "issue", "view", returncode=1)

    assert _resolve(client, row) == {}
    probe = load_ticket_state_probes()[_key(row)]
    assert probe.checked_at == old
    assert probe.attempted_at == _T0


@pytest.mark.parametrize("tracker", ["linear", None])
def test_a_non_github_tracker_is_never_read(
    root: Path, cli: FakeGhCli, tracker: str | None
) -> None:
    _register_debt_client(root, name=_CLIENT, tracker=tracker)
    other = ClientConfig(name=_CLIENT, workspace_path=root, default_branch="main")

    assert _resolve(other, _row()) == {}
    assert cli.records == []


# --- resolve: cap, budget, backoff -------------------------------------------


def test_the_read_cap_defers_the_surplus_without_reading_it(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    rows = _staggered(_MAX_STATE_READS_PER_TICK + 3)

    hits = _resolve(client, *rows)

    assert len(cli.records) == _MAX_STATE_READS_PER_TICK
    surplus = rows[_MAX_STATE_READS_PER_TICK:]
    assert hits == {
        r.ticket_id: ClosedTicketHit(r.created_at, defer=True) for r in surplus
    }
    cached = load_ticket_state_probes()
    assert all(_key(r) not in cached for r in surplus)

    hits = _resolve(client, *rows, now=_T0 + timedelta(seconds=30))

    assert hits == {}
    assert _read_ids(cli)[_MAX_STATE_READS_PER_TICK:] == [r.ticket_id for r in surplus]


def test_an_expired_open_row_cut_by_the_cap_is_deferred(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    rows = _staggered(_MAX_STATE_READS_PER_TICK + 1)
    last = rows[-1]
    old = _T0 - timedelta(seconds=_OPEN_STATE_TTL_SECONDS + 1)
    save_ticket_state_probes(
        _CLIENT,
        {_key(last): TicketStateProbe(checked_at=old, closed=False)},
        prune_to_keys=[_key(r) for r in rows],
    )

    hits = _resolve(client, *rows)

    # The expired entry was attempted most recently, so it reads last and is
    # the one the cap cuts: deferred, never defaulted to open.
    assert hits == {last.ticket_id: ClosedTicketHit(last.created_at, defer=True)}


def test_the_wall_clock_budget_defers_the_rest(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    rows = _staggered(4)
    ticks = iter([0.0, 1.0, 2.0, 30.0, 31.0, 32.0, 33.0])

    hits = _resolve(client, *rows, clock=lambda: next(ticks), budget_seconds=25.0)

    assert _read_ids(cli) == ["100", "101"]
    assert hits == {
        r.ticket_id: ClosedTicketHit(r.created_at, defer=True) for r in rows[2:]
    }
    cached = load_ticket_state_probes()
    assert all(_key(r) not in cached for r in rows[2:])


def test_failing_reads_do_not_starve_a_healthy_row(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    rows = _staggered(_MAX_STATE_READS_PER_TICK + 1)
    healthy = rows[-1]
    cli.reply("gh", "issue", "view", returncode=1)
    cli.reply("gh", "issue", "view", healthy.ticket_id, stdout=_CLOSED)

    first = _resolve(client, *rows)
    assert first == {healthy.ticket_id: ClosedTicketHit(healthy.created_at, True)}

    second = _resolve(client, *rows, now=_T0 + timedelta(seconds=30))

    assert second == {healthy.ticket_id: ClosedTicketHit(healthy.created_at, False)}
    assert _read_ids(cli)[-1] == healthy.ticket_id
    assert len(cli.records) == _MAX_STATE_READS_PER_TICK + 1


def test_an_expired_closed_verdict_is_re_read_before_cancelling(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    closed = _row()
    save_ticket_state_probes(
        _CLIENT,
        {_key(closed): TicketStateProbe(checked_at=_T0, closed=True)},
        prune_to_keys=[_key(closed)],
    )

    far = _T0 + timedelta(days=30)
    hits = _resolve(client, closed, now=far)

    assert hits == {}
    assert _read_ids(cli) == [closed.ticket_id]
    assert load_ticket_state_probes()[_key(closed)].closed is False


def test_the_deferral_logs_one_summary(
    client: ClientConfig, cli: FakeGhCli, caplog: pytest.LogCaptureFixture
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    with caplog.at_level(logging.INFO, logger="cw.dispatch"):
        _resolve(client, *_staggered(_MAX_STATE_READS_PER_TICK + 2))
    lines = [r for r in caplog.records if "deferred 2 candidate(s)" in r.getMessage()]
    assert len(lines) == 1


# --- resolve: ordering, lanes, filters -------------------------------------


def test_reads_follow_claim_order(client: ClientConfig, cli: FakeGhCli) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    rows = [
        _row("1", priority=0, created_at=_T0),
        _row("2", priority=5, created_at=_T0 + timedelta(seconds=2), lane="fast"),
        _row("3", priority=5, created_at=_T0 + timedelta(seconds=1)),
        _row("4", priority=0, created_at=_T0 - timedelta(seconds=1), lane="fast"),
    ]

    _resolve(client, *rows)

    assert _read_ids(cli) == ["3", "2", "4", "1"]


def test_priority_ids_are_read_first_in_list_order(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    rows = _staggered(4)

    _resolve(client, *rows, priority_ticket_ids=["103", "101"])

    assert _read_ids(cli) == ["103", "101", "100", "102"]


def test_a_row_in_a_lane_that_cannot_claim_is_never_read(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_CLOSED)

    hits = _resolve(client, _row("1", lane="blocked"), claimable_lanes={_LANE})

    assert hits == {}
    assert cli.records == []


def test_claimable_lane_names() -> None:
    lanes = [
        LaneConfig(name="paused", max_parallel=2, paused=True),
        LaneConfig(name="full", max_parallel=2),
        LaneConfig(name="room", max_parallel=2),
    ]

    names = claimable_lane_names(lanes, {"full": 2, "room": 1, "paused": 0})

    assert names == frozenset({"room"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"stage": Stage.REVIEW},
        {"stage": Stage.FINALIZE},
        {"status": QueueItemStatus.RUNNING},
        {"client": "other"},
        {"adopted_pr_url": "https://github.com/o/r/pull/1"},
        {"pr_url": "https://github.com/o/r/pull/2"},
        {"ticket_id": "GEN-9"},
    ],
    ids=["review", "finalize", "running", "other-client", "adopted", "pr-url", "gen"],
)
def test_non_candidates_are_not_read(
    client: ClientConfig, cli: FakeGhCli, overrides: dict[str, object]
) -> None:
    cli.reply("gh", "issue", "view", stdout=_CLOSED)

    assert _resolve(client, _row(**overrides)) == {}
    assert cli.records == []


def test_duplicate_ticket_rows_read_once(client: ClientConfig, cli: FakeGhCli) -> None:
    cli.reply("gh", "issue", "view", stdout=_CLOSED)
    first = _row("7")
    second = _row("7", created_at=_T0 + timedelta(seconds=5))

    hits = _resolve(client, second, first)

    assert hits == {"7": ClosedTicketHit(first.created_at, defer=False)}
    assert len(cli.records) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"pr_url": "https://github.com/o/r/pull/1"},
        {"adopted_pr_url": "https://github.com/o/r/pull/1"},
    ],
    ids=["pr-url-added", "adopted-pr-added"],
)
def test_closed_hit_is_ignored_if_pr_fields_changed_under_the_lock(
    overrides: dict[str, object],
) -> None:
    row = _row(**overrides)
    hit = ClosedTicketHit(row.created_at, defer=False)

    assert _live_closed_ticket_hit(row, {row.ticket_id: hit}) is None


def test_a_stamped_row_is_sticky_but_a_fresh_enqueue_is_read(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_CLOSED)
    requeued = _row("9")
    save_ticket_state_probes(
        _CLIENT,
        {_key(requeued): TicketStateProbe(_T0, closed=True, cancelled_at=_T0)},
        prune_to_keys=[_key(requeued)],
    )

    assert _resolve(client, requeued) == {}
    assert cli.records == []

    fresh = _row("9", created_at=_T0 + timedelta(hours=1))
    assert _resolve(client, fresh) == {"9": ClosedTicketHit(fresh.created_at, False)}
    assert len(cli.records) == 1


def test_prune_keeps_snapshot_rows_and_drops_vanished_ones(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    cancelled = _row("1", status=QueueItemStatus.CANCELLED)
    other_lane = _row("2", lane="blocked")
    gone = _row("3")
    keys = [_key(cancelled), _key(other_lane), _key(gone)]
    save_ticket_state_probes(
        _CLIENT,
        {k: TicketStateProbe(_T0, closed=True, cancelled_at=_T0) for k in keys},
        prune_to_keys=keys,
    )
    foreign = ticket_state_key("zeta", "1", _T0)
    save_ticket_state_probes(
        "zeta", {foreign: TicketStateProbe(_T0, closed=False)}, prune_to_keys=[foreign]
    )

    _resolve(client, cancelled, other_lane, _row("4"), claimable_lanes={_LANE})

    cached = load_ticket_state_probes()
    assert _key(cancelled) in cached
    assert _key(other_lane) in cached
    assert _key(gone) not in cached
    assert foreign in cached


def test_reads_are_scoped_and_lock_free(client: ClientConfig, cli: FakeGhCli) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)

    _resolve(client, _row())

    assert cli.records[0].argv == ["gh", "issue", "view", "101", "--json", "state"]
    assert cli.records[0].cwd == client.workspace_path
    assert cli.records[0].timeout == _STATE_READ_TIMEOUT_SECONDS
    assert cli.lock_snapshots
    assert all(snapshot == () for snapshot in cli.lock_snapshots)


# --- lanes wiring ------------------------------------------------------------


def test_lanes_wrapper_skips_a_saturated_client(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    hits = _resolve_closed_ticket_hits_if_gated(
        client,
        DevQueueStore(tasks=[_row()]),
        effective_lanes=[LaneConfig(name=_LANE, max_parallel=1)],
        running_by_lane={},
        priority_ids=None,
        available_client_slots=0,
    )

    assert hits == {}
    assert cli.records == []


def test_lanes_wrapper_passes_lane_context(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    rows = [_row("1"), _row("2", lane="fast"), _row("3", created_at=_T0 + timedelta(1))]

    _resolve_closed_ticket_hits_if_gated(
        client,
        DevQueueStore(tasks=rows),
        effective_lanes=[
            LaneConfig(name=_LANE, max_parallel=1),
            LaneConfig(name="fast", max_parallel=1),
        ],
        running_by_lane={"fast": 1},
        priority_ids=["3"],
        available_client_slots=2,
    )

    assert _read_ids(cli) == ["3", "1"]
    assert all(snapshot == () for snapshot in cli.lock_snapshots)


def test_dispatch_tick_cancels_a_closed_pending_row(
    root: Path,
    tmp_path: Path,
    cli: FakeGhCli,
    mock_native_daemon: Any,
) -> None:
    from cw.dispatch import dispatch_tick
    from tests._clients_yaml import write_clients_yaml

    _register_debt_client(root, name=_CLIENT, tracker="github-issues")
    write_clients_yaml(
        ClientConfig(
            name=_CLIENT,
            workspace_path=root,
            default_branch="main",
            worktree_base=tmp_path / "worktrees",
        )
    )
    cli.reply("gh", "issue", "view", stdout=_CLOSED)
    _seed(_row())

    result = dispatch_tick(
        config=OrchestratorConfig(
            tick_interval_seconds=30, per_client_max_parallel={_CLIENT: 1}
        ),
        native_daemon=mock_native_daemon,
    )

    assert result.spawned == 0
    assert mock_native_daemon.spawn_calls == []
    assert _stored("101").status == QueueItemStatus.CANCELLED
    issue_reads = [
        snap
        for record, snap in zip(cli.records, cli.lock_snapshots, strict=True)
        if record.argv[:3] == ["gh", "issue", "view"]
    ]
    assert issue_reads
    assert all(snap == () for snap in issue_reads)


# --- claim half --------------------------------------------------------------


def _seed(*rows: TicketTask) -> None:
    save_dev_queue(DevQueueStore(tasks=list(rows)))


def _stored(ticket_id: str) -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)


def _claim(
    client: ClientConfig,
    hits: dict[str, ClosedTicketHit] | None,
    **kwargs: Any,
) -> TicketTask | None:
    claimed, backoff = _claim_next_pending(
        _CLIENT,
        lane=kwargs.pop("lane", _LANE),
        client=client,
        config=OrchestratorConfig(
            tick_interval_seconds=30, per_client_max_parallel={_CLIENT: 4}
        ),
        closed_ticket_hits=hits,
        **kwargs,
    )
    assert backoff is False
    return claimed


def _closed(row: TicketTask) -> dict[str, ClosedTicketHit]:
    return {row.ticket_id: ClosedTicketHit(row.created_at, defer=False)}


def _ticks() -> list[dict[str, Any]]:
    return [
        e.payload
        for e in read_events(event_types=[OrchestratorEventType.DISPATCH_TICK])
    ]


def test_a_closed_hit_cancels_the_row_without_charging_it(
    client: ClientConfig,
) -> None:
    row = _row(attempts=2, unproductive_attempts=1, session_id="s-old")
    _seed(row)

    assert _claim(client, _closed(row)) is None

    stored = _stored("101")
    assert stored.status == QueueItemStatus.CANCELLED
    assert stored.session_id is None
    assert stored.attempts == 2
    assert stored.unproductive_attempts == 1
    ticks = [t for t in _ticks() if t.get("ticket_id") == "101"]
    assert ticks == [
        {
            "client": _CLIENT,
            "claimed": 0,
            "skip_reason": "ticket_closed",
            "ticket_id": "101",
            "cancel_reason": TICKET_CLOSED_CANCEL_REASON,
        }
    ]
    transitions = read_events(event_types=[OrchestratorEventType.TASK_TRANSITION])
    assert any(
        e.payload.get("old_status") == "pending"
        and e.payload.get("new_status") == "cancelled"
        for e in transitions
    )
    assert load_ticket_state_probes()[_key(row)].cancelled_at is not None


@pytest.mark.parametrize("priority", [None, ["101", "102"]])
def test_a_cancel_is_a_skip_so_the_next_row_claims(
    client: ClientConfig, priority: list[str] | None
) -> None:
    closed = _row("101")
    nxt = _row("102", created_at=_T0 + timedelta(seconds=1))
    _seed(closed, nxt)

    claimed = _claim(client, _closed(closed), priority_ticket_ids=priority)

    assert claimed is not None
    assert claimed.ticket_id == "102"
    assert _stored("101").status == QueueItemStatus.CANCELLED


def test_a_deferred_row_is_held_untouched_and_the_next_row_claims(
    client: ClientConfig,
) -> None:
    eligible = _T0 - timedelta(hours=1)
    held = _row(
        "101",
        attempts=3,
        unproductive_attempts=1,
        next_eligible_at=eligible,
        disposition="x",
    )
    nxt = _row("102", created_at=_T0 + timedelta(seconds=1))
    _seed(held)
    before = len(read_events())

    assert _claim(client, {"101": ClosedTicketHit(held.created_at, True)}) is None

    stored = _stored("101")
    assert stored.status == QueueItemStatus.PENDING
    assert stored.attempts == 3
    assert stored.unproductive_attempts == 1
    assert stored.next_eligible_at == eligible
    assert stored.disposition == "x"
    assert len(read_events()) == before

    _seed(held, nxt)
    claimed = _claim(client, {"101": ClosedTicketHit(held.created_at, True)})
    assert claimed is not None
    assert claimed.ticket_id == "102"
    assert _stored("101").status == QueueItemStatus.PENDING


def test_cap_deferred_rows_are_never_spawned_unchecked(
    client: ClientConfig, cli: FakeGhCli
) -> None:
    cli.reply("gh", "issue", "view", stdout=_OPEN)
    rows = _staggered(_MAX_STATE_READS_PER_TICK + 3)
    _seed(*rows)

    hits = _resolve(client, *load_dev_queue().tasks)
    claimed = [_claim(client, hits) for _ in range(len(rows))]

    read = {r.ticket_id for r in rows[:_MAX_STATE_READS_PER_TICK]}
    assert {t.ticket_id for t in claimed if t is not None} == read
    deferred = rows[_MAX_STATE_READS_PER_TICK:]
    assert all(_stored(r.ticket_id).status == QueueItemStatus.PENDING for r in deferred)

    hits = _resolve(client, *load_dev_queue().tasks, now=_T0 + timedelta(seconds=30))
    assert hits == {}
    again = [_claim(client, hits) for _ in range(3)]
    assert {t.ticket_id for t in again if t is not None} == {
        r.ticket_id for r in deferred
    }


def test_requeue_from_cancelled_sticks(client: ClientConfig, cli: FakeGhCli) -> None:
    cli.reply("gh", "issue", "view", stdout=_CLOSED)
    _seed(_row())
    hits = _resolve(client, *load_dev_queue().tasks)
    assert _claim(client, hits) is None
    cancelled = _stored("101")
    assert cancelled.status == QueueItemStatus.CANCELLED

    requeue_ticket("101", _CLIENT, from_cancelled=True)

    requeued = _stored("101")
    assert requeued.status == QueueItemStatus.PENDING
    assert requeued.created_at == cancelled.created_at
    hits = _resolve(client, *load_dev_queue().tasks, now=_T0 + timedelta(seconds=30))
    assert hits == {}
    claimed = _claim(client, hits)
    assert claimed is not None
    assert claimed.ticket_id == "101"


@pytest.mark.parametrize("defer", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        {"created_at": _T0 + timedelta(seconds=9)},
        {"stage": Stage.REVIEW},
        {"status": QueueItemStatus.RUNNING},
    ],
    ids=["new-identity", "stage-moved", "left-pending"],
)
def test_a_hit_is_ignored_when_the_row_changed(
    client: ClientConfig, change: dict[str, object], *, defer: bool
) -> None:
    snapshot_row = _row()
    _seed(_row(**change))
    hits = {"101": ClosedTicketHit(snapshot_row.created_at, defer=defer)}

    claimed = _claim(client, hits)

    stored = _stored("101")
    assert stored.status != QueueItemStatus.CANCELLED
    if change.get("status") is None:
        assert claimed is not None
        assert stored.status == QueueItemStatus.RUNNING


def test_a_failed_stamp_keeps_the_cancel_and_warns(
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = _row()
    _seed(row)

    def _boom(*_a: object, **_k: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.dispatch_state_ticket_state.atomic_write_text", _boom)
    with caplog.at_level(logging.WARNING):
        assert _claim(client, _closed(row)) is None

    assert _stored("101").status == QueueItemStatus.CANCELLED
    assert "failed to stamp ticket_state_probe" in caplog.text


def test_the_cancel_warning_names_the_requeue(
    client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    row = _row()
    _seed(row)
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        _claim(client, _closed(row))
    assert "cw dev-queue requeue 101 -c acme --from-cancelled" in caplog.text


@pytest.mark.parametrize("hits", [None, {}])
def test_no_hits_screens_nothing(
    client: ClientConfig, hits: dict[str, ClosedTicketHit] | None
) -> None:
    _seed(_row())

    claimed = _claim(client, hits)

    assert claimed is not None
    assert _stored("101").status == QueueItemStatus.RUNNING


def test_an_occupied_worktree_screens_first(client: ClientConfig) -> None:
    row = _row()
    _seed(row)

    claimed = _claim(
        client, _closed(row), occupied_ticket_reasons={"101": "live session s-1"}
    )

    assert claimed is None
    assert _stored("101").status == QueueItemStatus.PENDING


def test_module_constants_are_exported() -> None:
    assert gate.TICKET_CLOSED_CANCEL_REASON == "ticket_closed"
    assert json.loads(_CLOSED)["state"] == gate._GH_ISSUE_STATE_CLOSED
