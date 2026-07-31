"""Tests for cw_operator_events's session.needs_attention digest coalescer.

RFC 0011 A6 (#1162). A held (``HOLD_DISPOSITIONS``) park is buffered on
``TicketTask.attention_digest_buffered_at`` instead of forwarded immediately;
every other admitted event -- including a non-held or ticketless
``session.needs_attention`` -- still forwards exactly as before, unbatched.
The buffer flushes to a single digest SSE push per batch, gated on the
local-timezone delivery window (``OrchestratorConfig.attention_digest_window_
tz``/``_start_hour``/``_end_hour``) and an idle-drain floor
(``attention_digest_idle_floor_seconds``) anchored to the most recent
arrival in the currently-buffered batch, not the oldest -- see
``_peek_flushable_digest``'s docstring.

Sibling to ``tests/test_cw_operator_events.py`` (RFC 0008 W3, #1002), split
into its own file per R2/R3's corrected anchor -- the digest logic lives in
``cw_operator_events.py``, not ``reconcile/tasks.py`` -- and to avoid growing
the existing ~586-line file past ~1000 lines.

Sibling coverage lives in ``tests/test_dev_queue.py``:
``test_cancel_clears_attention_digest_buffer_marker`` (transition_task_status's
unconditional clear) and ``test_migrate_dev_queue_fills_attention_digest_
buffered_default`` / ``test_v24_attention_digest_buffered_at_preserved_
idempotently`` (schema migration) -- colocated with their sibling
escalation/gate-recipe/hold-finalize-latch tests rather than duplicated here.
"""

from __future__ import annotations

import json
import queue
from collections.abc import Generator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import freezegun
import pytest
from pydantic import ValidationError

import cw.cw_operator_events as _operator_mod
from cw.cw_operator_events import (
    _in_delivery_window,
    poll_and_forward_operator_channel,
    subscribe,
    unsubscribe,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dev_queue.lifecycle import (
    AWAITING_OPERATOR_DISPOSITION,
    transition_task_status,
)
from cw.events import record_event
from cw.models import (
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

# 2026-01-15 15:00 UTC == 10:00 EST -- inside the default 08:00-20:00 window.
_INSIDE_WINDOW = "2026-01-15T15:00:00+00:00"
# 2026-01-15 03:00 UTC == 22:00 EST (previous day) -- outside the window.
_OUTSIDE_WINDOW = "2026-01-15T03:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset_operator_subscribers() -> Generator[None]:
    """Clear the operator-channel subscriber list between tests.

    Copied from test_cw_operator_events.py's identical fixture: subscriber
    state lives on the shared _operator_mod module object, so every test file
    that calls subscribe()/poll_and_forward_operator_channel needs its own
    autouse reset -- a module-scoped fixture in one file does not reach tests
    in another.
    """
    with _operator_mod._lock:
        _operator_mod._subscribers.clear()
    yield
    with _operator_mod._lock:
        _operator_mod._subscribers.clear()


@pytest.fixture(autouse=True)
def _reset_operator_channel_state() -> Generator[None]:
    """Reset durable-replay in-memory state between tests (see above)."""
    with _operator_mod._file_lock:
        _operator_mod._cursors.clear()
        _operator_mod._event_offset[0] = 0
    yield
    with _operator_mod._file_lock:
        _operator_mod._cursors.clear()
        _operator_mod._event_offset[0] = 0


def _held_task(ticket_id: str, client: str = "acme", **overrides: object) -> TicketTask:
    """A TicketTask parked in the RFC 0011 A1 hold class (digest-eligible)."""
    kwargs: dict[str, object] = {
        "ticket_id": ticket_id,
        "client": client,
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": AWAITING_OPERATOR_DISPOSITION,
    }
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def _attention_event(ticket_id: str | None, client: str | None = "acme") -> None:
    """Record a session.needs_attention event for *ticket_id*/*client*."""
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        payload={
            "ticket_id": ticket_id,
            "client": client,
            "paused_status": "awaiting_operator_availability",
        },
        correlation_id=ticket_id,
    )


def _drain(q: queue.SimpleQueue[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pop every currently-queued notification off *q*."""
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


# ---------------------------------------------------------------------------
# TestDigestBuffering -- buffer-vs-forward classification
# ---------------------------------------------------------------------------


class TestDigestBuffering:
    def test_single_held_park_buffers_not_forwards(self, tmp_events_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1")]))
        _attention_event("T-1")
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW):
                poll_and_forward_operator_channel(OrchestratorConfig())
            assert q.empty()
        finally:
            unsubscribe(q)
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "T-1")
        assert task.attention_digest_buffered_at is not None

    def test_two_held_parks_coalesce_into_one_digest(
        self, tmp_events_dir: Path
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1"), _held_task("T-2")]))
        with freezegun.freeze_time(_INSIDE_WINDOW) as frozen:
            _attention_event("T-1")
            _attention_event("T-2")
            poll_and_forward_operator_channel(OrchestratorConfig())

            q = subscribe()
            try:
                frozen.tick(delta=timedelta(seconds=61))
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
                assert len(items) == 1
                data = json.loads(items[0]["message"])
                assert data["digest"] is True
                assert data["count"] == 2
                assert {e["ticket_id"] for e in data["entries"]} == {"T-1", "T-2"}
            finally:
                unsubscribe(q)

    def test_r7_mixed_class_scenario(self, tmp_events_dir: Path) -> None:
        """held T0, blocked T+10s, held T+20s, flush well past the idle floor
        anchored to the LAST held arrival -> the blocked event forwards
        immediately and unbatched; both held events land in exactly ONE
        digest (R7's required test, verbatim)."""
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _held_task("T-HELD-1"),
                    _held_task("T-HELD-2"),
                    _make_ticket_task(
                        ticket_id="T-BLOCKED",
                        client="acme",
                        status=QueueItemStatus.BLOCKED_ON_USER,
                        disposition="scope_exceeded",
                    ),
                ]
            )
        )
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW) as frozen:
                _attention_event("T-HELD-1")
                poll_and_forward_operator_channel(OrchestratorConfig())
                assert q.empty()

                frozen.tick(delta=timedelta(seconds=10))
                _attention_event("T-BLOCKED")
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
                assert len(items) == 1
                assert json.loads(items[0]["message"])["ticket_id"] == "T-BLOCKED"

                frozen.tick(delta=timedelta(seconds=10))
                _attention_event("T-HELD-2")
                poll_and_forward_operator_channel(OrchestratorConfig())
                assert q.empty()

                frozen.tick(delta=timedelta(seconds=61))
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
                assert len(items) == 1
                data = json.loads(items[0]["message"])
                assert data["digest"] is True
                assert {e["ticket_id"] for e in data["entries"]} == {
                    "T-HELD-1",
                    "T-HELD-2",
                }
        finally:
            unsubscribe(q)

    def test_disposition_not_in_hold_dispositions_forwards_immediately(
        self, tmp_events_dir: Path
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="T-1",
                        client="acme",
                        status=QueueItemStatus.BLOCKED_ON_USER,
                        disposition="scope_exceeded",
                    )
                ]
            )
        )
        _attention_event("T-1")
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW):
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
            assert len(items) == 1
            assert json.loads(items[0]["message"])["ticket_id"] == "T-1"
        finally:
            unsubscribe(q)

    def test_missing_task_for_ticket_id_forwards_immediately(
        self, tmp_events_dir: Path
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        _attention_event("T-GHOST")
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW):
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
            assert len(items) == 1
            assert json.loads(items[0]["message"])["ticket_id"] == "T-GHOST"
        finally:
            unsubscribe(q)

    def test_ticketless_event_never_buffered(self, tmp_events_dir: Path) -> None:
        """A fleet-wide session.needs_attention (no owning ticket, e.g.
        gating.py's gh_availability_outage shape) always forwards immediately
        -- it can never resolve to a held task."""
        _attention_event(None, client=None)
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW):
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
            assert len(items) == 1
            assert (
                json.loads(items[0]["message"])["paused_status"]
                == "awaiting_operator_availability"
            )
        finally:
            unsubscribe(q)


# ---------------------------------------------------------------------------
# TestDigestWindowAndIdleFloor -- flush gating
# ---------------------------------------------------------------------------


class TestDigestWindowAndIdleFloor:
    def test_window_closed_never_flushes_regardless_of_age(
        self, tmp_events_dir: Path
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1")]))
        q = subscribe()
        try:
            with freezegun.freeze_time(_OUTSIDE_WINDOW) as frozen:
                _attention_event("T-1")
                poll_and_forward_operator_channel(OrchestratorConfig())
                frozen.tick(delta=timedelta(hours=5))
                poll_and_forward_operator_channel(OrchestratorConfig())
            assert q.empty()
        finally:
            unsubscribe(q)

    def test_window_open_flushes_full_overnight_batch(
        self, tmp_events_dir: Path
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1"), _held_task("T-2")]))
        q = subscribe()
        try:
            with freezegun.freeze_time(_OUTSIDE_WINDOW) as frozen:
                _attention_event("T-1")
                poll_and_forward_operator_channel(OrchestratorConfig())
                frozen.tick(delta=timedelta(hours=1))
                _attention_event("T-2")
                poll_and_forward_operator_channel(OrchestratorConfig())
                assert q.empty()

                frozen.move_to(_INSIDE_WINDOW)
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
            assert len(items) == 1
            data = json.loads(items[0]["message"])
            assert data["count"] == 2
        finally:
            unsubscribe(q)

    def test_idle_floor_not_yet_elapsed_no_flush(self, tmp_events_dir: Path) -> None:
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1")]))
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW) as frozen:
                _attention_event("T-1")
                poll_and_forward_operator_channel(OrchestratorConfig())
                frozen.tick(delta=timedelta(seconds=30))
                poll_and_forward_operator_channel(OrchestratorConfig())
            assert q.empty()
        finally:
            unsubscribe(q)

    def test_new_arrival_resets_idle_drain(self, tmp_events_dir: Path) -> None:
        """The idle-drain floor is anchored to the NEWEST buffered arrival,
        not the oldest -- a fresh held park pushes the flush back out. A
        naive oldest-arrival check would already flush at T+90 (90s since
        T-1's T0 arrival); this asserts it does NOT, because only 45s have
        elapsed since T-2's T+45 arrival."""
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1"), _held_task("T-2")]))
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW) as frozen:
                _attention_event("T-1")
                poll_and_forward_operator_channel(OrchestratorConfig())

                frozen.tick(delta=timedelta(seconds=45))
                _attention_event("T-2")
                poll_and_forward_operator_channel(OrchestratorConfig())

                frozen.tick(delta=timedelta(seconds=45))
                poll_and_forward_operator_channel(OrchestratorConfig())
                assert q.empty()

                frozen.tick(delta=timedelta(seconds=20))
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
            assert len(items) == 1
            assert json.loads(items[0]["message"])["count"] == 2
        finally:
            unsubscribe(q)

    def test_nothing_held_at_flush_sends_nothing(self, tmp_events_dir: Path) -> None:
        """R9: an empty held set at flush time sends nothing -- no '0 items'
        ping."""
        save_dev_queue(DevQueueStore(tasks=[]))
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW) as frozen:
                poll_and_forward_operator_channel(OrchestratorConfig())
                frozen.tick(delta=timedelta(minutes=5))
                poll_and_forward_operator_channel(OrchestratorConfig())
            assert q.empty()
        finally:
            unsubscribe(q)

    def test_ticket_resolved_before_flush_excluded_from_digest(
        self, tmp_events_dir: Path
    ) -> None:
        """R9: a ticket resolved between buffering and flush is excluded --
        the digest re-derives live state, never replays the buffered
        episode."""
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1"), _held_task("T-2")]))
        with freezegun.freeze_time(_INSIDE_WINDOW) as frozen:
            _attention_event("T-1")
            _attention_event("T-2")
            poll_and_forward_operator_channel(OrchestratorConfig())

            store = load_dev_queue()
            resolved = next(t for t in store.tasks if t.ticket_id == "T-1")
            transition_task_status(
                resolved, QueueItemStatus.COMPLETED, disposition="shipped"
            )
            save_dev_queue(store)

            q = subscribe()
            try:
                frozen.tick(delta=timedelta(seconds=61))
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
            finally:
                unsubscribe(q)
        # Resolving T-1 itself emits a real task.transition notification (an
        # unrelated, always-forwarded event type) -- filter down to the
        # digest push specifically rather than asserting total item count.
        digests = [json.loads(i["message"]) for i in items]
        digests = [d for d in digests if d.get("digest") is True]
        assert len(digests) == 1
        assert {e["ticket_id"] for e in digests[0]["entries"]} == {"T-2"}

    def test_crash_restart_preserves_buffer(self, tmp_events_dir: Path) -> None:
        """R8: a persisted buffer marker survives a fresh load_dev_queue()
        call -- simulating a process restart mid-window with nothing lost."""
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1")]))
        with freezegun.freeze_time(_INSIDE_WINDOW):
            _attention_event("T-1")
            poll_and_forward_operator_channel(OrchestratorConfig())
        reloaded = load_dev_queue()
        task = next(t for t in reloaded.tasks if t.ticket_id == "T-1")
        assert task.attention_digest_buffered_at is not None

    def test_digest_content_lists_all_held_tickets(self, tmp_events_dir: Path) -> None:
        """Explicit ticket acceptance criterion: the digest content lists
        all held tickets, with the exact committed 3-key per-entry schema and
        an uncapped top-level count for a batch large enough (3) that a
        hidden cap would be detectable."""
        tasks = [
            _held_task("T-1", blocked_reason="operator unavailable"),
            _held_task("T-2", blocked_reason=None),
            _held_task("T-3", blocked_reason="ssh key gate bypassed"),
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        q = subscribe()
        try:
            with freezegun.freeze_time(_INSIDE_WINDOW) as frozen:
                for t in tasks:
                    _attention_event(t.ticket_id)
                poll_and_forward_operator_channel(OrchestratorConfig())
                frozen.tick(delta=timedelta(seconds=61))
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
        finally:
            unsubscribe(q)
        assert len(items) == 1
        data = json.loads(items[0]["message"])
        assert data["count"] == 3
        by_id = {e["ticket_id"]: e for e in data["entries"]}
        assert set(by_id) == {"T-1", "T-2", "T-3"}
        for entry in by_id.values():
            assert set(entry) == {"ticket_id", "client", "breadcrumbs"}
        assert by_id["T-1"]["breadcrumbs"] == "operator unavailable"
        assert by_id["T-2"]["breadcrumbs"] is None
        assert by_id["T-3"]["client"] == "acme"

    def test_is_held_recheck_excludes_task_without_marker_clear(
        self, tmp_events_dir: Path
    ) -> None:
        """R9's live _is_held() recheck inside _peek_flushable_digest fires
        independent of transition_task_status's marker-clear side effect --
        distinguishes this from test_ticket_resolved_before_flush_excluded_
        from_digest above, which goes through transition_task_status and so
        cannot tell whether the exclusion comes from the recheck or from the
        marker simply being zeroed. Here disposition is flipped directly
        (bypassing transition_task_status entirely) while
        attention_digest_buffered_at is left set, so the only thing that can
        explain T-1's exclusion below is _peek_flushable_digest's own
        _is_held(task) check at flush time."""
        save_dev_queue(DevQueueStore(tasks=[_held_task("T-1"), _held_task("T-2")]))
        with freezegun.freeze_time(_INSIDE_WINDOW) as frozen:
            _attention_event("T-1")
            _attention_event("T-2")
            poll_and_forward_operator_channel(OrchestratorConfig())

            store = load_dev_queue()
            t1 = next(t for t in store.tasks if t.ticket_id == "T-1")
            t1.disposition = "scope_exceeded"
            assert t1.attention_digest_buffered_at is not None
            save_dev_queue(store)

            q = subscribe()
            try:
                frozen.tick(delta=timedelta(seconds=61))
                poll_and_forward_operator_channel(OrchestratorConfig())
                items = _drain(q)
            finally:
                unsubscribe(q)
        assert len(items) == 1
        data = json.loads(items[0]["message"])
        assert {e["ticket_id"] for e in data["entries"]} == {"T-2"}


# ---------------------------------------------------------------------------
# TestDeliveryWindowDST -- R12 local-timezone window resolution
# ---------------------------------------------------------------------------


class TestDeliveryWindowDST:
    def test_dst_flush_inside_window_edt(self) -> None:
        """10:00 local during EDT (UTC-4) resolves inside the window."""
        config = OrchestratorConfig()
        now = datetime.fromisoformat("2026-07-15T14:00:00+00:00")  # 10:00 EDT
        assert _in_delivery_window(config, now) is True

    def test_dst_same_wall_clock_hour_est(self) -> None:
        """The SAME local wall-clock hour (10:00), but on a date in EST
        (UTC-5) -- proves the check is local-hour-based, not a fixed UTC
        offset (a fixed-offset implementation would disagree with the EDT
        case above at the same local hour)."""
        config = OrchestratorConfig()
        now = datetime.fromisoformat("2026-01-15T15:00:00+00:00")  # 10:00 EST
        assert _in_delivery_window(config, now) is True

    def test_dst_boundary_flush_decision_each_side(self) -> None:
        """The 2026 US DST spring-forward transition instant is 07:00:00 UTC
        on 2026-03-08 (01:59:59 EST -> 03:00:00 EDT; the 2am local hour is
        skipped entirely). Both sides land outside the digest window -- the
        in-window decision must agree despite the discontinuous local-hour
        jump across the transition."""
        config = OrchestratorConfig()
        just_before = datetime.fromisoformat("2026-03-08T06:59:00+00:00")
        just_after = datetime.fromisoformat("2026-03-08T07:01:00+00:00")
        assert _in_delivery_window(config, just_before) is False
        assert _in_delivery_window(config, just_after) is False

    def test_localized_hour_diverges_from_raw_utc_hour(self) -> None:
        """A regression that silently dropped the .astimezone(tz) call and
        compared the bare UTC hour instead would NOT be caught by
        test_dst_flush_inside_window_edt/test_dst_same_wall_clock_hour_est/
        test_dst_boundary_flush_decision_each_side above -- every instant
        those tests use happens to land on the same side of the 8-20 window
        whether or not real timezone conversion occurs. This test picks an
        instant where the two disagree: 23:30 UTC on 2026-07-15 is 19:30 EDT
        -- raw UTC hour 23 is OUTSIDE the 8-20 window, but the correctly
        localized hour 19 is INSIDE it. Only a real astimezone(tz) conversion
        passes this."""
        config = OrchestratorConfig()
        now = datetime.fromisoformat("2026-07-15T23:30:00+00:00")  # 19:30 EDT
        assert _in_delivery_window(config, now) is True

    def test_invalid_timezone_fails_loud_at_config_load(self) -> None:
        """R12c: an unresolvable IANA zone raises ValidationError at config
        construction -- never a silent fallback to UTC."""
        with pytest.raises(ValidationError):
            OrchestratorConfig(attention_digest_window_tz="Not/AZone")

    def test_start_hour_gte_end_hour_fails_loud_at_config_load(self) -> None:
        """A start_hour >= end_hour window can never open -- _in_delivery_
        window's start <= hour < end predicate would be false for every hour
        of every day, silently dropping every digest forever. Must fail loud
        at config construction, same fail-loud stance as the timezone
        validator above, not ship as a silently-dead window."""
        with pytest.raises(ValidationError):
            OrchestratorConfig(
                attention_digest_window_start_hour=20,
                attention_digest_window_end_hour=8,
            )

    def test_start_hour_equal_end_hour_fails_loud_at_config_load(self) -> None:
        """The equal-bounds edge case (start == end) is just as dead as
        start > end -- also must fail loud, not silently construct a
        zero-width window."""
        with pytest.raises(ValidationError):
            OrchestratorConfig(
                attention_digest_window_start_hour=8,
                attention_digest_window_end_hour=8,
            )
