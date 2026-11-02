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
    _CARRYOVER,
    MAX_STOP_JOBS_PER_TICK,
    DeferredReconcileJobs,
    PostLockJob,
    StopKind,
    defer_surface_stop,
    is_surface_stop_queued,
    run_post_lock_jobs,
)
from tests._reconcile_helpers import use_reconcile_daemon


def _recording_job(
    label: str, calls: list[str], *, stop: StopKind = StopKind.NOT_A_STOP
) -> PostLockJob:
    return PostLockJob(label=label, run=lambda: calls.append(label), stop=stop)


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


def _stop_labels(count: int, *, prefix: str = "s") -> list[str]:
    return [f"surface_stop:{prefix}{index}" for index in range(count)]


def _stop_jobs(
    labels: list[str], calls: list[str], kind: StopKind
) -> list[PostLockJob]:
    return [_recording_job(label, calls, stop=kind) for label in labels]


class TestStopCap:
    """#2551: the post-lock drain runs at most MAX_STOP_JOBS_PER_TICK stops."""

    def test_cap_constant_is_five(self) -> None:
        assert MAX_STOP_JOBS_PER_TICK == 5

    def test_cap_boundary_exactly_n_runs_all_and_logs_no_drop(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []
        labels = _stop_labels(MAX_STOP_JOBS_PER_TICK)
        sink = DeferredReconcileJobs(
            post_lock=_stop_jobs(labels, calls, StopKind.RECOVERABLE)
        )

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.deferred"):
            run_post_lock_jobs(sink)

        assert calls == labels
        assert "post_lock_stop_cap" not in caplog.text

    def test_cap_boundary_n_plus_one_drops_the_surplus_and_logs_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []
        labels = _stop_labels(MAX_STOP_JOBS_PER_TICK + 1)
        sink = DeferredReconcileJobs(
            post_lock=_stop_jobs(labels, calls, StopKind.RECOVERABLE)
        )

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.deferred"):
            run_post_lock_jobs(sink)

        assert calls == labels[:MAX_STOP_JOBS_PER_TICK]
        assert (
            f"post_lock_stop_cap_dropped count=1 cap={MAX_STOP_JOBS_PER_TICK}"
            in caplog.text
        )
        assert f"labels={labels[-1]}" in caplog.text

    def test_recoverable_surplus_is_not_carried_to_the_next_drain(self) -> None:
        calls: list[str] = []
        labels = _stop_labels(3)
        sink = DeferredReconcileJobs(
            post_lock=_stop_jobs(labels, calls, StopKind.RECOVERABLE)
        )

        run_post_lock_jobs(sink, max_stops=1)
        assert calls == [labels[0]]
        assert _CARRYOVER.labels() == frozenset()

        run_post_lock_jobs(DeferredReconcileJobs(), max_stops=1)

        assert calls == [labels[0]]

    def test_carried_surplus_runs_first_on_the_next_drain(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []
        carried = _stop_labels(3, prefix="c")
        sink = DeferredReconcileJobs(
            post_lock=_stop_jobs(carried, calls, StopKind.CARRIED)
        )

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.deferred"):
            run_post_lock_jobs(sink, max_stops=1)

        assert calls == [carried[0]]
        assert "post_lock_stop_cap_carried count=2 cap=1" in caplog.text
        assert f"labels={carried[1]},{carried[2]}" in caplog.text

        fresh = _recording_job("surface_stop:new", calls, stop=StopKind.RECOVERABLE)
        run_post_lock_jobs(DeferredReconcileJobs(post_lock=[fresh]), max_stops=2)

        # Carried jobs run first (FIFO) and fill the cap; the fresh recoverable
        # stop is the surplus and is dropped.
        assert calls == carried

    def test_carried_surplus_counts_against_next_cap(self) -> None:
        calls: list[str] = []
        carried = _stop_labels(2, prefix="c")
        run_post_lock_jobs(
            DeferredReconcileJobs(
                post_lock=_stop_jobs(carried, calls, StopKind.CARRIED)
            ),
            max_stops=1,
        )
        assert calls == [carried[0]]
        calls.clear()
        fresh = _recording_job("surface_stop:new", calls, stop=StopKind.RECOVERABLE)

        run_post_lock_jobs(DeferredReconcileJobs(post_lock=[fresh]), max_stops=1)

        assert calls == [carried[1]]

    def test_non_stop_jobs_always_run_when_stop_cap_exhausted(self) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                _recording_job("stop-a", calls, stop=StopKind.RECOVERABLE),
                _recording_job("gh-1", calls),
                _recording_job("stop-b", calls, stop=StopKind.RECOVERABLE),
                _recording_job("gh-2", calls),
                _recording_job("stop-c", calls, stop=StopKind.CARRIED),
                _recording_job("gh-3", calls),
            ]
        )

        run_post_lock_jobs(sink, max_stops=1)

        assert calls == ["stop-a", "gh-1", "gh-2", "gh-3"]

    def test_non_stop_jobs_are_not_counted_toward_the_cap(self) -> None:
        calls: list[str] = []
        non_stops = [f"gh-{index}" for index in range(MAX_STOP_JOBS_PER_TICK * 2)]
        stops = _stop_labels(MAX_STOP_JOBS_PER_TICK)
        sink = DeferredReconcileJobs(
            post_lock=[
                *(_recording_job(label, calls) for label in non_stops),
                *_stop_jobs(stops, calls, StopKind.RECOVERABLE),
            ]
        )

        run_post_lock_jobs(sink)

        assert calls == [*non_stops, *stops]

    def test_max_stops_override_zero_runs_no_stops_but_all_others(self) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                _recording_job("stop-a", calls, stop=StopKind.RECOVERABLE),
                _recording_job("gh-1", calls),
            ]
        )

        run_post_lock_jobs(sink, max_stops=0)

        assert calls == ["gh-1"]

    def test_raising_stop_is_isolated_and_counts_toward_the_cap(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(
                    label="surface_stop:boom",
                    run=_raise_os_error,
                    stop=StopKind.RECOVERABLE,
                ),
                _recording_job("surface_stop:b", calls, stop=StopKind.RECOVERABLE),
                _recording_job("surface_stop:c", calls, stop=StopKind.RECOVERABLE),
            ]
        )

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.deferred"):
            run_post_lock_jobs(sink, max_stops=2)

        assert calls == ["surface_stop:b"]
        assert "post_lock_job_failed label=surface_stop:boom" in caplog.text
        assert "post_lock_stop_cap_dropped count=1 cap=2" in caplog.text

    def test_raising_non_stop_job_still_isolated(self) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(label="gh-boom", run=_raise_os_error),
                _recording_job("after", calls),
            ]
        )

        run_post_lock_jobs(sink)

        assert calls == ["after"]

    def test_keyboard_interrupt_still_propagates(self) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(
                    label="surface_stop:int",
                    run=_raise_keyboard_interrupt,
                    stop=StopKind.RECOVERABLE,
                ),
                _recording_job("surface_stop:b", calls, stop=StopKind.RECOVERABLE),
                _recording_job("surface_stop:c", calls, stop=StopKind.RECOVERABLE),
            ]
        )

        with pytest.raises(KeyboardInterrupt):
            run_post_lock_jobs(sink, max_stops=2)

        assert calls == []

    def test_interrupted_drain_recarries_unrun_carried_stops(self) -> None:
        calls: list[str] = []
        carried = _stop_labels(2, prefix="c")
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(
                    label="surface_stop:int",
                    run=_raise_keyboard_interrupt,
                    stop=StopKind.RECOVERABLE,
                ),
                *_stop_jobs(carried, calls, StopKind.CARRIED),
                _recording_job("surface_stop:rec", calls, stop=StopKind.RECOVERABLE),
            ]
        )

        with pytest.raises(KeyboardInterrupt):
            run_post_lock_jobs(sink, max_stops=10)

        assert calls == []
        assert _CARRYOVER.labels() == frozenset(carried)

    def test_interrupted_drain_recarried_stops_run_ahead_of_surplus_carry(
        self,
    ) -> None:
        calls: list[str] = []
        carried = _stop_labels(3, prefix="c")
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(
                    label="surface_stop:int",
                    run=_raise_keyboard_interrupt,
                    stop=StopKind.RECOVERABLE,
                ),
                *_stop_jobs(carried, calls, StopKind.CARRIED),
            ]
        )

        # Cap 3 admits the interrupting stop and the first two CARRIED stops;
        # the third CARRIED stop is the surplus put back before the loop runs.
        with pytest.raises(KeyboardInterrupt):
            run_post_lock_jobs(sink, max_stops=3)

        assert [job.label for job in _CARRYOVER.take()] == carried

    def test_interrupted_drain_with_no_carried_jobs_leaves_carryover_empty(
        self,
    ) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                PostLockJob(
                    label="surface_stop:int",
                    run=_raise_keyboard_interrupt,
                    stop=StopKind.RECOVERABLE,
                ),
                _recording_job("surface_stop:rec", calls, stop=StopKind.RECOVERABLE),
                _recording_job("gh-1", calls),
            ]
        )

        with pytest.raises(KeyboardInterrupt):
            run_post_lock_jobs(sink)

        assert calls == []
        assert _CARRYOVER.labels() == frozenset()

    def test_dropped_count_logs_only_when_nonzero(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=[
                _recording_job("gh-1", calls),
                *_stop_jobs(_stop_labels(2), calls, StopKind.RECOVERABLE),
            ]
        )

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.deferred"):
            run_post_lock_jobs(sink, max_stops=2)

        assert "post_lock_stop_cap" not in caplog.text


class TestCarryOver:
    def test_is_surface_stop_queued_sees_carried_stop(self) -> None:
        calls: list[str] = []
        sink = DeferredReconcileJobs(
            post_lock=_stop_jobs(
                ["surface_stop:a", "surface_stop:b"], calls, StopKind.CARRIED
            )
        )
        run_post_lock_jobs(sink, max_stops=1)

        assert is_surface_stop_queued(DeferredReconcileJobs(), "b")
        assert not is_surface_stop_queued(DeferredReconcileJobs(), "a")

    def test_carried_stop_not_requeued_by_defer_surface_stop(self) -> None:
        calls: list[str] = []
        run_post_lock_jobs(
            DeferredReconcileJobs(
                post_lock=_stop_jobs(
                    ["surface_stop:a", "surface_stop:b"], calls, StopKind.CARRIED
                )
            ),
            max_stops=1,
        )
        sink = DeferredReconcileJobs()

        defer_surface_stop(sink, "b")

        assert sink.post_lock == []

    def test_carry_is_cleared_by_take(self) -> None:
        calls: list[str] = []
        run_post_lock_jobs(
            DeferredReconcileJobs(
                post_lock=_stop_jobs(
                    ["surface_stop:a", "surface_stop:b"], calls, StopKind.CARRIED
                )
            ),
            max_stops=1,
        )

        taken = _CARRYOVER.take()

        assert [job.label for job in taken] == ["surface_stop:b"]
        assert _CARRYOVER.take() == []
        assert _CARRYOVER.labels() == frozenset()


class TestDeferSurfaceStopKind:
    def test_default_kind_is_recoverable(self) -> None:
        sink = DeferredReconcileJobs()

        defer_surface_stop(sink, "abcd1234")

        assert sink.post_lock[0].stop is StopKind.RECOVERABLE

    def test_kind_override(self) -> None:
        sink = DeferredReconcileJobs()

        defer_surface_stop(sink, "abcd1234", kind=StopKind.CARRIED)

        assert sink.post_lock[0].stop is StopKind.CARRIED

    def test_post_lock_job_defaults_to_not_a_stop(self) -> None:
        assert PostLockJob(label="x", run=lambda: None).stop is StopKind.NOT_A_STOP
