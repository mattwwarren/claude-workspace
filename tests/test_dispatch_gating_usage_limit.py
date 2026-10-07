"""Usage-limit reconcile preamble tests for ``dispatch_tick``.

Covers ``cw.dispatch.gating.usage_limit``: a reconcile failure
is contained, and a held ``.sessions.lock`` skips the tick (#2491).
Split out of ``tests/test_dispatch.py`` (#2503).
"""

from __future__ import annotations

import logging

import pytest

from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
from cw.config import sessions_lock_file
from cw.dev_queue import (
    add_ticket,
    load_dev_queue,
)
from cw.dispatch import dispatch_tick
from cw.dispatch.gating import _reconcile_usage_limited
from cw.events import read_events
from cw.exceptions import SessionsLockTimeoutError
from cw.models import (
    ClientConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml
from tests.conftest import _hold_sessions_lock


class TestDispatchTickReconcileErrors:
    """Reconcile failure inside dispatch_tick is contained, not propagated.

    Paired test for the sanctioned BLE001 broad-catch at
    src/cw/dispatch.py:105. Reconcile is best-effort housekeeping; if it
    fails (transient adapter outage, corrupted roster, OSError on stale
    socket), dispatch_tick must log + continue, not propagate.
    """

    def test_reconcile_failure_does_not_crash_dispatch_tick(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        write_clients_yaml(sample_client_config)

        def _boom_reconcile(*_args: object, **_kwargs: object) -> None:
            msg = "simulated reconcile failure"
            raise RuntimeError(msg)

        # Patch the name as imported into cw.dispatch (not cw.reconcile).
        monkeypatch.setattr("cw.dispatch.gating.usage_limit.reconcile", _boom_reconcile)

        daemon = FakeNativeDaemonClient()

        caplog.set_level(logging.ERROR, logger="cw.dispatch")

        # Must not raise; reconcile guard catches and logs, dispatch_tick
        # continues to the dev-queue scan and returns normally.
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        assert any(
            "reconcile failed" in record.getMessage().lower()
            for record in caplog.records
            if record.name == "cw.dispatch" and record.levelno >= logging.ERROR
        ), "expected ERROR log from cw.dispatch mentioning 'reconcile failed'"

    @pytest.mark.parametrize("usage_limited", [True, False])
    def test_dispatch_loop_reconcile_opts_in_to_review_job_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_limited: bool,
    ) -> None:
        """#1229: the live dispatch loop's reconcile preamble is the ONE caller
        that lets reconcile() fire the address_review / auto_fix_ci recipes
        (cw status / list / start / doctor call it with the False default)."""
        from cw.dispatch.gating import _reconcile_usage_limited
        from cw.reconcile import ReconcileReport

        seen: list[dict[str, object]] = []

        def _record_reconcile(**kwargs: object) -> ReconcileReport:
            seen.append(kwargs)
            return ReconcileReport(usage_limited=usage_limited)

        monkeypatch.setattr(
            "cw.dispatch.gating.usage_limit.reconcile", _record_reconcile
        )

        assert _reconcile_usage_limited() is usage_limited
        assert seen == [{"dispatch_review_jobs": True}]


class TestDispatchTickSessionsLockTimeout:
    """A held ``.sessions.lock`` skips the tick; it never crashes serve (#2491)."""

    def test_reconcile_lock_timeout_skips_tick_with_warning(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-2491", client="test-client"))

        def _timeout(*_args: object, **_kwargs: object) -> None:
            msg = "lock held"
            raise SessionsLockTimeoutError(
                msg, lock_path=sessions_lock_file(), waited_s=60.0
            )

        monkeypatch.setattr("cw.dispatch.gating.usage_limit.reconcile", _timeout)
        daemon = FakeNativeDaemonClient()
        caplog.set_level(logging.WARNING, logger="cw.dispatch")

        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        assert not result.usage_limit_detected
        assert daemon.spawn_calls == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "GEN-2491")
        assert task.status == QueueItemStatus.PENDING  # not claimed this tick
        skip_warnings = [
            record
            for record in caplog.records
            if record.name == "cw.dispatch" and "skipping tick" in record.getMessage()
        ]
        assert len(skip_warnings) == 1  # logged once, with the exception text
        assert skip_warnings[0].levelno == logging.WARNING
        assert "lock held" in skip_warnings[0].getMessage()

    def test_skipped_tick_records_no_dispatch_tick_event(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DISPATCH_TICK is written after claim/spawn, so a skipped tick has none.

        This is the premise of the watchdog claim in ``tick.py``: the gap is
        visible only through the watchdogs that read ``dispatch.tick`` age.
        """
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-2493", client="test-client"))

        def _timeout(*_args: object, **_kwargs: object) -> None:
            msg = "lock held"
            raise SessionsLockTimeoutError(
                msg, lock_path=sessions_lock_file(), waited_s=60.0
            )

        with monkeypatch.context() as patch_ctx:
            patch_ctx.setattr("cw.dispatch.gating.usage_limit.reconcile", _timeout)
            dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert read_events(event_types=[OrchestratorEventType.DISPATCH_TICK]) == []

        # Positive control: with reconcile working again the same tick records
        # exactly one event (one client), so the empty list above is not vacuous.
        dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert len(read_events(event_types=[OrchestratorEventType.DISPATCH_TICK])) == 1

    def test_real_contention_skips_tick_then_next_tick_proceeds(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Real contention end to end: held lock -> skipped tick; released -> spawn."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-2492", client="test-client"))
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.2")
        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        daemon = FakeNativeDaemonClient()

        with _hold_sessions_lock():
            skipped = dispatch_tick(simple_config, native_daemon=daemon)

        assert skipped.spawned == 0
        assert daemon.spawn_calls == []
        assert any("skipping tick" in record.getMessage() for record in caplog.records)
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-2492")
        assert task.status == QueueItemStatus.PENDING  # nothing claimed or spawned
        assert read_events(event_types=[OrchestratorEventType.DISPATCH_TICK]) == []

        retried = dispatch_tick(simple_config, native_daemon=daemon)

        assert retried.spawned == 1
        # Positive control: the very same setup DOES record a tick once it
        # proceeds (one per client), so the empty list above is not vacuous.
        assert len(read_events(event_types=[OrchestratorEventType.DISPATCH_TICK])) == 1

    def test_other_reconcile_errors_still_swallowed(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the lock timeout escapes the guard; the broad catch is intact."""
        write_clients_yaml(sample_client_config)

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "simulated reconcile failure"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.dispatch.gating.usage_limit.reconcile", _boom)

        assert not _reconcile_usage_limited()
