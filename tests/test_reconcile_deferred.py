"""Unit tests for cw.reconcile.deferred (#1232).

The post-lock job sink ``reconcile()`` threads through ``_reconcile_locked``:
act phases queue external calls (daemon surface stops) under
``sessions_lock`` and ``run_post_lock_jobs`` runs them after the lock
releases, isolating each job's failure.
"""

from __future__ import annotations

import logging

import pytest

from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.deferred import (
    DeferredReconcileJobs,
    PostLockJob,
    defer_surface_stop,
    is_surface_stop_queued,
    run_post_lock_jobs,
)
from tests._reconcile_helpers import use_reconcile_daemon


def _recording_job(label: str, calls: list[str]) -> PostLockJob:
    return PostLockJob(label=label, run=lambda: calls.append(label))


def _raise_os_error() -> None:
    msg = "daemon socket gone"
    raise OSError(msg)


def _raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt


class TestDeferSurfaceStop:
    def test_stop_is_not_called_until_the_drain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeNativeDaemonClient()
        use_reconcile_daemon(monkeypatch, daemon)
        sink = DeferredReconcileJobs()

        defer_surface_stop(sink, "abcd1234")

        assert daemon.stop_calls == []
        assert [job.label for job in sink.post_lock] == ["surface_stop:abcd1234"]

        run_post_lock_jobs(sink)

        assert daemon.stop_calls == ["abcd1234"]

    def test_daemon_is_resolved_at_drain_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        defer_time = FakeNativeDaemonClient()
        drain_time = FakeNativeDaemonClient()
        use_reconcile_daemon(monkeypatch, defer_time)
        sink = DeferredReconcileJobs()
        defer_surface_stop(sink, "abcd1234")

        use_reconcile_daemon(monkeypatch, drain_time)
        run_post_lock_jobs(sink)

        assert defer_time.stop_calls == []
        assert drain_time.stop_calls == ["abcd1234"]

    def test_same_ref_twice_queues_one_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeNativeDaemonClient()
        use_reconcile_daemon(monkeypatch, daemon)
        sink = DeferredReconcileJobs()

        defer_surface_stop(sink, "abcd1234")
        defer_surface_stop(sink, "abcd1234")
        run_post_lock_jobs(sink)

        assert len(sink.post_lock) == 1
        assert daemon.stop_calls == ["abcd1234"]

    def test_distinct_refs_each_queue_a_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeNativeDaemonClient()
        use_reconcile_daemon(monkeypatch, daemon)
        sink = DeferredReconcileJobs()

        defer_surface_stop(sink, "aaaa1111")
        defer_surface_stop(sink, "bbbb2222")
        run_post_lock_jobs(sink)

        assert daemon.stop_calls == ["aaaa1111", "bbbb2222"]


class TestIsSurfaceStopQueued:
    def test_false_on_empty_sink(self) -> None:
        assert not is_surface_stop_queued(DeferredReconcileJobs(), "abcd1234")

    def test_true_only_for_the_queued_ref(self) -> None:
        sink = DeferredReconcileJobs()
        defer_surface_stop(sink, "abcd1234")

        assert is_surface_stop_queued(sink, "abcd1234")
        assert not is_surface_stop_queued(sink, "ffff0000")

    def test_ignores_a_non_stop_job_with_the_bare_ref_as_label(self) -> None:
        sink = DeferredReconcileJobs(
            post_lock=[PostLockJob(label="abcd1234", run=lambda: None)]
        )

        assert not is_surface_stop_queued(sink, "abcd1234")


class TestRunPostLockJobs:
    def test_empty_sink_is_a_no_op(self) -> None:
        sink = DeferredReconcileJobs()

        run_post_lock_jobs(sink)

        assert sink.post_lock == []
        assert sink.review is None

    def test_jobs_run_in_insertion_order(self) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[_recording_job(label, calls) for label in ("c", "a", "b")]
        )

        run_post_lock_jobs(sink)

        assert calls == ["c", "a", "b"]

    def test_two_jobs_with_an_identical_label_both_run(self) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[_recording_job("same", calls), _recording_job("same", calls)]
        )

        run_post_lock_jobs(sink)

        assert calls == ["same", "same"]

    def test_raising_job_is_logged_and_later_jobs_still_run(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(label="boom", run=_raise_os_error),
                _recording_job("after", calls),
            ]
        )

        with caplog.at_level(logging.ERROR, logger="cw.reconcile.deferred"):
            run_post_lock_jobs(sink)

        assert calls == ["after"]
        failures = [
            r for r in caplog.records if "post_lock_job_failed label=" in r.message
        ]
        assert len(failures) == 1
        assert failures[0].getMessage() == "post_lock_job_failed label=boom"
        assert failures[0].exc_info is not None
        assert failures[0].exc_info[0] is OSError

    def test_keyboard_interrupt_propagates_and_abandons_later_jobs(self) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(label="interrupt", run=_raise_keyboard_interrupt),
                _recording_job("after", calls),
            ]
        )

        with pytest.raises(KeyboardInterrupt):
            run_post_lock_jobs(sink)

        assert calls == []

    def test_job_return_value_is_ignored(self) -> None:
        """A job wrapping a helper that returns a value (e.g. an acted ticket
        id) is accepted and its result discarded; the drain moves on."""
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(label="returns", run=lambda: "GEN-1"),
                _recording_job("after", calls),
            ]
        )

        run_post_lock_jobs(sink)

        assert calls == ["after"]
        assert [job.label for job in sink.post_lock] == ["returns", "after"]
