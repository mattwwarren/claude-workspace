"""Tests for cw.reconcile.fix_dispatch_hold (#2590, refs #2502).

A fix-loop row whose fix worker launched but was never recorded carries a
``fix_dispatch_launched_worker`` tombstone. The completions phase of
``cw.reconcile.fix_dispatch`` unparks it only once a readable daemon roster no
longer lists the launched surface; until then the row stays RUNNING and held,
and pages after a grace period.

Tests 1-17 drive ``fix_dispatch._act_on_fix_dispatch_completions`` (fed by the
real detect phase) as the integration seam; test 18 drives the public cancel
and requeue functions. Seeding and doc parsing go through the shared helpers in
``tests/_reconcile_helpers.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.dev_queue import cancel_ticket, load_dev_queue, requeue_ticket, save_dev_queue
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
)
from cw.native_daemon import RealNativeDaemonClient
from cw.queue_rows import _is_fix_dispatch_held
from cw.reconcile import fix_dispatch, fix_dispatch_hold
from cw.reconcile.fix_dispatch_hold import (
    FIX_DISPATCH_WORKER_UNCONFIRMED_REASON,
    HoldDecision,
    apply_launched_worker_hold,
)
from tests._clients_yaml import write_clients_yaml
from tests._reconcile_helpers import _FIX_LOOP_CLIENT as _CLIENT
from tests._reconcile_helpers import _FIX_LOOP_TICKET as _TICKET
from tests._reconcile_helpers import (
    _make_launched_fix_worker,
    _only_task,
    _save_fix_session,
    _seed_task,
    events_doc_bullet,
    use_reconcile_daemon,
)
from tests.conftest import _CANONICAL_ATTENTION_KEYS, _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import LaunchedFixWorker
    from cw.native_daemon import FakeNativeDaemonClient
    from tests.conftest import CapturedEvent

_LOGGER = "cw.reconcile.fix_dispatch_hold"
_MODULE = "cw.reconcile.fix_dispatch_hold"
_SESSION = "fix-sess"
_LAUNCHED = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
_RELEASE_LOG = "fix_dispatch_worker_confirmed_stopped"


@pytest.fixture
def daemon(
    mock_native_daemon: FakeNativeDaemonClient, monkeypatch: pytest.MonkeyPatch
) -> FakeNativeDaemonClient:
    """The fake daemon, installed at reconcile's ``_deps`` patch point."""
    use_reconcile_daemon(monkeypatch, mock_native_daemon)
    return mock_native_daemon


@pytest.fixture
def pages(
    capture_events: Callable[..., list[CapturedEvent]],
) -> list[CapturedEvent]:
    """Every ``SESSION_NEEDS_ATTENTION`` the hold module emits."""
    return capture_events(_MODULE, OrchestratorEventType.SESSION_NEEDS_ATTENTION)


def _seed_held(worker: LaunchedFixWorker, **overrides: object) -> None:
    """Seed the fix-loop row carrying *worker* as its tombstone."""
    _seed_task(
        fix_dispatch_session_id=_SESSION,
        fix_dispatch_launched_worker=worker,
        **overrides,
    )


def _run_completions() -> list[str]:
    """Run the real detect phase into the completions act phase."""
    return fix_dispatch._act_on_fix_dispatch_completions(
        fix_dispatch._detect_fix_dispatch_completions(load_dev_queue().tasks)
    )


def _assert_held(worker: LaunchedFixWorker) -> None:
    task = _only_task()
    assert task.status == QueueItemStatus.RUNNING
    assert task.fix_dispatch_session_id == _SESSION
    current = task.fix_dispatch_launched_worker
    assert current is not None
    assert current.surface_ref == worker.surface_ref
    assert current.launched_at == worker.launched_at


def _release_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _LOGGER and r.getMessage().startswith(_RELEASE_LOG)
    ]


# --- 1-3: the hold itself ---------------------------------------------------


def test_holds_when_roster_lists_surface(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    """1: the launched surface is still in the roster, so the row stays held."""
    worker = _make_launched_fix_worker(surface_ref=daemon.seed_live_worker(tmp_path))
    _seed_held(worker)

    assert _run_completions() == []

    _assert_held(worker)
    assert daemon.stop_calls == []


def test_holds_when_roster_unreadable(daemon: FakeNativeDaemonClient) -> None:
    """2: an unreadable roster cannot confirm absence, so the row stays held."""
    daemon.roster_unreadable = True
    worker = _make_launched_fix_worker()
    _seed_held(worker)

    assert _run_completions() == []

    _assert_held(worker)
    assert daemon.stop_calls == []


def test_holds_when_roster_file_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """2b: the native reader maps an absent roster to set(); the hold must not."""
    client = RealNativeDaemonClient(roster_path=tmp_path / "absent-roster.json")
    assert client.list_live_session_short_ids_fail_closed() == set()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: client)
    worker = _make_launched_fix_worker()
    _seed_held(worker)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _run_completions() == []

    _assert_held(worker)
    assert _release_records(caplog) == []
    assert any(
        r.name == _LOGGER and "daemon roster file is absent" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("method", OSError("roster read failed")),
        ("method", ValueError("roster is not valid JSON")),
        ("client", OSError("daemon client construction failed")),
    ],
    ids=["read-oserror", "read-valueerror", "client-construction-oserror"],
)
def test_roster_read_oserror_fails_closed(
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    error: Exception,
) -> None:
    """3: a roster read (or client construction) that raises holds the row."""

    def _raise(*_args: object) -> object:
        raise error

    if target == "method":
        monkeypatch.setattr(daemon, "list_live_session_short_ids_fail_closed", _raise)
    else:
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", _raise)
    worker = _make_launched_fix_worker()
    _seed_held(worker)

    assert fix_dispatch_hold.read_live_worker_roster() is None
    assert _run_completions() == []

    _assert_held(worker)


# --- 4-8: paging -------------------------------------------------------------


def test_silent_inside_grace(
    daemon: FakeNativeDaemonClient, pages: list[CapturedEvent]
) -> None:
    """4: no page while the launch is younger than the 5 minute grace."""
    daemon.roster_unreadable = True
    _seed_held(_make_launched_fix_worker(launched_at=_LAUNCHED))

    with freeze_time(_LAUNCHED + timedelta(minutes=4)):
        assert _run_completions() == []

    assert pages == []
    worker = _only_task().fix_dispatch_launched_worker
    assert worker is not None
    assert worker.attention_paged_at is None


def test_pages_after_grace_with_canonical_payload(
    tmp_path: Path, daemon: FakeNativeDaemonClient, pages: list[CapturedEvent]
) -> None:
    """5: past the grace the hold pages once with the canonical payload, and
    persists the page instant even though nothing unparked (save-on-dirty)."""
    surface = daemon.seed_live_worker(tmp_path)
    _seed_held(
        _make_launched_fix_worker(surface_ref=surface, launched_at=_LAUNCHED),
        lane="fast",
    )
    now = _LAUNCHED + timedelta(minutes=6)

    with freeze_time(now):
        assert _run_completions() == []

    assert len(pages) == 1
    etype, payload, correlation_id = pages[0]
    assert etype == OrchestratorEventType.SESSION_NEEDS_ATTENTION
    assert set(payload) == _CANONICAL_ATTENTION_KEYS
    task = _only_task()
    assert payload["paused_status"] == "fix_dispatch_worker_unconfirmed"
    assert payload["paused_status"] == FIX_DISPATCH_WORKER_UNCONFIRMED_REASON
    assert payload["session_id"] == task.fix_dispatch_session_id
    assert payload["session_name"] == ""
    assert payload["client"] == task.client
    assert payload["lane"] == task.lane == "fast"
    assert payload["ticket_id"] == _TICKET
    assert payload["crashed"] is False
    assert payload["claude_session_id"] is None
    assert correlation_id == _TICKET
    worker = task.fix_dispatch_launched_worker
    assert worker is not None
    assert worker.attention_paged_at == now


@pytest.mark.parametrize("roster", ["listed", "unreadable"])
def test_breadcrumbs_name_the_clearing_action(
    tmp_path: Path,
    daemon: FakeNativeDaemonClient,
    pages: list[CapturedEvent],
    roster: str,
) -> None:
    """6: each breadcrumb names only the action that releases the row."""
    if roster == "listed":
        surface = daemon.seed_live_worker(tmp_path)
    else:
        surface = "abc12345"
        daemon.roster_unreadable = True
    _seed_held(_make_launched_fix_worker(surface_ref=surface, launched_at=_LAUNCHED))

    with freeze_time(_LAUNCHED + timedelta(minutes=6)):
        _run_completions()

    assert len(pages) == 1
    breadcrumbs = pages[0][1]["breadcrumbs"]
    assert surface in breadcrumbs
    if roster == "listed":
        assert f"claude stop {surface}" in breadcrumbs
        assert "the entry itself is stale" in breadcrumbs
        assert "repair" in breadcrumbs
        assert "roster unreadable" not in breadcrumbs
    else:
        assert "roster unreadable" in breadcrumbs
        assert "repair" in breadcrumbs
        assert "restore" not in breadcrumbs
        assert "claude stop" not in breadcrumbs


def test_does_not_repage_inside_interval(
    daemon: FakeNativeDaemonClient, pages: list[CapturedEvent]
) -> None:
    """7: a page younger than the 60 minute interval is not repeated."""
    daemon.roster_unreadable = True
    now = _LAUNCHED + timedelta(hours=3)
    paged_at = now - timedelta(minutes=59)
    _seed_held(
        _make_launched_fix_worker(launched_at=_LAUNCHED, attention_paged_at=paged_at)
    )

    with freeze_time(now):
        assert _run_completions() == []

    assert pages == []
    worker = _only_task().fix_dispatch_launched_worker
    assert worker is not None
    assert worker.attention_paged_at == paged_at


def test_repages_after_interval(
    daemon: FakeNativeDaemonClient, pages: list[CapturedEvent]
) -> None:
    """8: past the 60 minute interval the hold pages again."""
    daemon.roster_unreadable = True
    now = _LAUNCHED + timedelta(hours=3)
    _seed_held(
        _make_launched_fix_worker(
            launched_at=_LAUNCHED, attention_paged_at=now - timedelta(minutes=61)
        )
    )

    with freeze_time(now):
        assert _run_completions() == []

    assert len(pages) == 1
    worker = _only_task().fix_dispatch_launched_worker
    assert worker is not None
    assert worker.attention_paged_at == now


# --- 9-12: release and the recorded-session interplay -------------------------


def test_unparks_and_clears_tombstone_when_surface_absent(
    daemon: FakeNativeDaemonClient, caplog: pytest.LogCaptureFixture
) -> None:
    """9: a readable roster without the surface releases the row and clears
    the tombstone, logging the confirmation once at WARNING."""
    _seed_held(_make_launched_fix_worker())

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert _run_completions() == [_TICKET]

    task = _only_task()
    assert task.fix_dispatch_session_id is None
    assert task.fix_dispatch_launched_worker is None
    assert task.status == QueueItemStatus.PENDING
    assert task.unproductive_attempts == 0
    records = _release_records(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == (
        "fix_dispatch_worker_confirmed_stopped ticket=2017 client=acme surface=abc12345"
    )
    assert records[0].levelno == logging.WARNING


def test_recorded_terminal_session_with_tombstone_needs_roster_confirm(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    """10: a recorded COMPLETED session does not release a tombstoned row by
    itself; the roster must confirm the surface gone first."""
    surface = daemon.seed_live_worker(tmp_path)
    worker = _make_launched_fix_worker(surface_ref=surface)
    _seed_held(worker)
    _save_fix_session(SessionStatus.COMPLETED)

    assert _run_completions() == []
    _assert_held(worker)

    daemon.stop(surface)

    assert _run_completions() == [_TICKET]
    task = _only_task()
    assert task.status == QueueItemStatus.PENDING
    assert task.fix_dispatch_launched_worker is None


def test_recorded_live_session_holds_without_paging(
    tmp_path: Path, daemon: FakeNativeDaemonClient, pages: list[CapturedEvent]
) -> None:
    """11: a recorded non-terminal session holds through the existing check
    and never pages, even long after the grace."""
    surface = daemon.seed_live_worker(tmp_path)
    worker = _make_launched_fix_worker(surface_ref=surface, launched_at=_LAUNCHED)
    _seed_held(worker)
    _save_fix_session(SessionStatus.ACTIVE)

    with freeze_time(_LAUNCHED + timedelta(hours=2)):
        assert _run_completions() == []

    _assert_held(worker)
    assert pages == []


def test_tombstone_less_row_never_reads_roster(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """12: without any tombstone the roster is never read, and a plain row
    unparks exactly as before #2590."""

    def _forbidden() -> object:
        msg = "roster must not be read for a tombstone-less batch"
        raise AssertionError(msg)

    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", _forbidden)
    _seed_task(fix_dispatch_session_id=_SESSION)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert _run_completions() == [_TICKET]

    assert _only_task().status == QueueItemStatus.PENDING
    assert _release_records(caplog) == []


@pytest.mark.parametrize("change", ["surface-swapped", "tombstone-appeared"])
def test_identity_recheck_skips_changed_row(
    daemon: FakeNativeDaemonClient, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """13: the row is re-checked under the lock against the detect snapshot;
    a row whose tombstone changed since detect is left untouched this tick."""
    if change == "surface-swapped":
        _seed_held(_make_launched_fix_worker())
        candidates = fix_dispatch._detect_fix_dispatch_completions(
            load_dev_queue().tasks
        )
        swapped = _make_launched_fix_worker(surface_ref="feed0002")

        def _swap_then_read() -> set[str]:
            store = load_dev_queue()
            store.tasks[0].fix_dispatch_launched_worker = swapped
            save_dev_queue(store)
            return set()

        monkeypatch.setattr(
            daemon, "list_live_session_short_ids_fail_closed", _swap_then_read
        )
    else:
        _seed_held(_make_launched_fix_worker())
        candidates = [
            fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)
        ]
        swapped = _make_launched_fix_worker()

    assert fix_dispatch._act_on_fix_dispatch_completions(candidates) == []

    task = _only_task()
    assert task.status == QueueItemStatus.RUNNING
    assert task.fix_dispatch_session_id == _SESSION
    assert task.fix_dispatch_launched_worker == swapped


# --- 14: docs -------------------------------------------------------------------


def test_docs_name_the_release_action() -> None:
    """14: docs/events.md names the release action for the new paused_status,
    and the spawn_post_launch_failed bullet points at it."""
    bullet = " ".join(
        events_doc_bullet('- `"fix_dispatch_worker_unconfirmed"`').split()
    )
    first_sentence = bullet.split(". ", 1)[0]
    assert "claude stop <surface_ref>" in first_sentence
    assert "roster unreadable" in first_sentence
    assert "5 minutes" in bullet
    assert "60 minutes" in bullet
    assert "the entry itself is stale" in bullet

    post_launch = " ".join(events_doc_bullet('- `"spawn_post_launch_failed"`').split())
    assert "fix_dispatch_worker_unconfirmed" in post_launch
    assert "lane slot" in post_launch


# --- 15-17: emit failure, batches, the decision unit ----------------------------


@pytest.mark.parametrize(
    "error", [OSError("inbox full"), CwError("inbox lock")], ids=["oserror", "cwerror"]
)
def test_emit_failure_still_holds_and_retries_next_tick(
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """15: a failed page never escapes the tick, keeps the row held, and leaves
    ``attention_paged_at`` unset so the next tick retries."""

    def _raise(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(f"{_MODULE}.record_event", _raise)
    daemon.roster_unreadable = True
    worker = _make_launched_fix_worker(launched_at=_LAUNCHED)
    _seed_held(worker)

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER),
        freeze_time(_LAUNCHED + timedelta(minutes=6)),
    ):
        assert _run_completions() == []

    _assert_held(worker)
    current = _only_task().fix_dispatch_launched_worker
    assert current is not None
    assert current.attention_paged_at is None
    assert any(
        "fix_dispatch_worker_unconfirmed_page_failed" in r.getMessage()
        and r.levelno == logging.WARNING
        for r in caplog.records
    )


def test_mixed_batch_holds_tombstoned_and_unparks_plain(
    tmp_path: Path, daemon: FakeNativeDaemonClient
) -> None:
    """16: one batch holds the tombstoned row and unparks the plain one, and
    the save fires for the plain row's unpark (inside the grace, so the held
    row writes nothing of its own)."""
    surface = daemon.seed_live_worker(tmp_path)
    worker = _make_launched_fix_worker(surface_ref=surface, launched_at=_LAUNCHED)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id=_TICKET,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    fix_dispatch_session_id=_SESSION,
                    fix_dispatch_launched_worker=worker,
                ),
                _make_ticket_task(
                    ticket_id="2018",
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    fix_dispatch_session_id="plain-sess",
                ),
            ]
        )
    )

    with freeze_time(_LAUNCHED + timedelta(minutes=1)):
        assert _run_completions() == ["2018"]

    by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    held = by_ticket[_TICKET]
    assert held.status == QueueItemStatus.RUNNING
    assert held.fix_dispatch_session_id == _SESSION
    assert held.fix_dispatch_launched_worker == worker
    plain = by_ticket["2018"]
    assert plain.status == QueueItemStatus.PENDING
    assert plain.fix_dispatch_session_id is None


def test_hold_decision_unit(pages: list[CapturedEvent]) -> None:
    """17: every return branch of ``apply_launched_worker_hold``."""
    now = _LAUNCHED + timedelta(minutes=6)
    plain = _make_ticket_task(fix_dispatch_session_id=_SESSION)

    # No tombstone: a plain row releases; a tombstone that vanished since
    # detect is skipped this tick.
    assert apply_launched_worker_hold(plain, None, None, now=now) == HoldDecision(
        release=True, dirty=False
    )
    assert apply_launched_worker_hold(
        plain, "abc12345", set(), now=now
    ) == HoldDecision(release=False, dirty=False)

    # Identity mismatch: held, nothing written.
    swapped = _make_ticket_task(
        fix_dispatch_session_id=_SESSION,
        fix_dispatch_launched_worker=_make_launched_fix_worker(surface_ref="feed0003"),
    )
    assert apply_launched_worker_hold(
        swapped, "abc12345", set(), now=now
    ) == HoldDecision(release=False, dirty=False)
    assert pages == []

    # Readable roster without the surface: release, tombstone cleared.
    gone = _make_ticket_task(
        fix_dispatch_session_id=_SESSION,
        fix_dispatch_launched_worker=_make_launched_fix_worker(launched_at=_LAUNCHED),
    )
    assert apply_launched_worker_hold(
        gone, "abc12345", {"feed0004"}, now=now
    ) == HoldDecision(release=True, dirty=True)
    assert gone.fix_dispatch_launched_worker is None

    # Still listed: held; pages once due (dirty), not again inside the interval.
    listed = _make_ticket_task(
        fix_dispatch_session_id=_SESSION,
        fix_dispatch_launched_worker=_make_launched_fix_worker(launched_at=_LAUNCHED),
    )
    assert apply_launched_worker_hold(
        listed, "abc12345", {"abc12345"}, now=now
    ) == HoldDecision(release=False, dirty=True)
    assert len(pages) == 1
    assert apply_launched_worker_hold(
        listed, "abc12345", {"abc12345"}, now=now + timedelta(minutes=1)
    ) == HoldDecision(release=False, dirty=False)
    assert len(pages) == 1


# --- 18: cancel + requeue --from-cancelled does not release ---------------------


def test_cancel_then_requeue_from_cancelled_leaves_row_held(
    tmp_path: Path, mock_native_daemon: FakeNativeDaemonClient
) -> None:
    """18: ``cancel`` then ``requeue --from-cancelled`` moves the row back to
    PENDING but keeps the hold: the hold follows ``fix_dispatch_session_id``
    and the tombstone, not the row's status (decision B)."""
    write_clients_yaml(
        ClientConfig(name=_CLIENT, workspace_path=tmp_path / "ws"),
        ensure_workspaces=True,
    )
    worker = _make_launched_fix_worker()
    _seed_task(fix_dispatch_session_id=_SESSION, fix_dispatch_launched_worker=worker)

    cancel_ticket(_TICKET, _CLIENT)

    assert _only_task().status == QueueItemStatus.CANCELLED
    assert _is_fix_dispatch_held(_only_task())

    requeue_ticket(
        _TICKET, _CLIENT, from_cancelled=True, native_daemon=mock_native_daemon
    )

    row = _only_task()
    assert row.status == QueueItemStatus.PENDING
    assert row.fix_dispatch_session_id == _SESSION
    assert row.fix_dispatch_launched_worker == worker
    assert _is_fix_dispatch_held(row)
