"""Concurrency-safety tests for DISPATCH_STATE_FILE writes (#1256).

Regression tests proving the lost-update race on the shared dispatch-state
sidecar (read-merge-write across ``save_usage_limited_until``,
``save_availability_probe_cache``, and ``save_main_drift_latches``) is fixed
by ``dispatch_state_lock()``. Modeled on ``tests/test_clients_lock.py``.

Note: ``fcntl.flock`` is advisory and locks are associated with the *open
file description*, not the file path or the process. Two threads in the
same process each doing their own ``open()`` + ``flock()`` on the same path
still serialize against each other — each call gets its own file
description, and the kernel still enforces mutual exclusion between them.
So this test suite validates real cross-writer serialization, not a no-op.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from cw.dispatch_state import (
    AvailabilityProbeCache,
    ExecutorBlockedMarker,
    load_availability_probe_cache,
    load_executor_blocked_markers,
    load_usage_limited_until,
    save_availability_probe_cache,
    save_executor_blocked_marker,
    save_main_drift_latches,
    save_usage_limited_until,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestDispatchStateLockConcurrency:
    def test_concurrent_availability_and_usage_limit_writes_both_survive(
        self, tmp_config_dir: Path
    ) -> None:
        """Thread A writes usage_limited_until, thread B writes availability_probe.

        Barrier-synchronized so both threads race on the read-merge-write
        window; both keys must survive (neither write may clobber the
        other's key, per #1157's contract — now additionally serialized by
        dispatch_state_lock()).
        """
        barrier = threading.Barrier(2)
        errors: list[Exception] = []
        future = datetime.now(UTC) + timedelta(hours=1)
        probed_at = datetime.now(UTC)

        def write_usage_limit() -> None:
            barrier.wait()
            try:
                save_usage_limited_until(future)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def write_probe_cache() -> None:
            barrier.wait()
            try:
                save_availability_probe_cache(
                    AvailabilityProbeCache(
                        probed_at=probed_at, available=True, latched=False
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=write_usage_limit)
        t2 = threading.Thread(target=write_probe_cache)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"save_* raised: {errors}"

        loaded_limit = load_usage_limited_until()
        assert loaded_limit is not None
        assert abs((loaded_limit - future).total_seconds()) < 1

        loaded_cache = load_availability_probe_cache()
        assert loaded_cache is not None
        assert loaded_cache.available is True
        assert loaded_cache.latched is False

    def test_concurrent_writes_across_all_three_save_functions_all_survive(
        self, tmp_config_dir: Path
    ) -> None:
        """One thread per save function; all three keys survive concurrently."""
        barrier = threading.Barrier(3)
        errors: list[Exception] = []
        future = datetime.now(UTC) + timedelta(hours=2)
        probed_at = datetime.now(UTC)
        latches = {"acme": True, "beta": False}

        def write_usage_limit() -> None:
            barrier.wait()
            try:
                save_usage_limited_until(future)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def write_probe_cache() -> None:
            barrier.wait()
            try:
                save_availability_probe_cache(
                    AvailabilityProbeCache(
                        probed_at=probed_at, available=False, latched=True
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def write_main_drift_latches() -> None:
            barrier.wait()
            try:
                save_main_drift_latches(latches)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=write_usage_limit),
            threading.Thread(target=write_probe_cache),
            threading.Thread(target=write_main_drift_latches),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"save_* raised: {errors}"

        loaded_limit = load_usage_limited_until()
        assert loaded_limit is not None
        assert abs((loaded_limit - future).total_seconds()) < 1

        loaded_cache = load_availability_probe_cache()
        assert loaded_cache is not None
        assert loaded_cache.available is False
        assert loaded_cache.latched is True

        import cw.dispatch_state

        raw = json.loads(cw.dispatch_state.DISPATCH_STATE_FILE.read_text())
        assert raw["main_drift_latches"] == latches

    def test_concurrent_marker_and_usage_limit_writes_both_survive(
        self, tmp_config_dir: Path
    ) -> None:
        """Thread A writes usage_limited_until, thread B writes an executor marker.

        Same barrier-synchronized shape as the availability-probe test above,
        for the fourth sidecar writer added by #1742.
        """
        barrier = threading.Barrier(2)
        errors: list[Exception] = []
        future = datetime.now(UTC) + timedelta(hours=1)
        marker = ExecutorBlockedMarker(
            client="acme",
            ticket_id="1723",
            executor="codex",
            reviewer_role=None,
            started_at=datetime.now(UTC),
            session_id="sid-1723",
        )

        def write_usage_limit() -> None:
            barrier.wait()
            try:
                save_usage_limited_until(future)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def write_marker() -> None:
            barrier.wait()
            try:
                save_executor_blocked_marker(marker)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=write_usage_limit)
        t2 = threading.Thread(target=write_marker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"save_* raised: {errors}"

        loaded_limit = load_usage_limited_until()
        assert loaded_limit is not None
        assert abs((loaded_limit - future).total_seconds()) < 1

        markers = load_executor_blocked_markers()
        assert "acme/1723" in markers
        assert markers["acme/1723"].executor == "codex"

    @pytest.mark.parametrize("n_threads", [5, 10])
    def test_many_concurrent_dispatch_state_writes_all_survive(
        self, tmp_config_dir: Path, n_threads: int
    ) -> None:
        """N threads round-robin across the three save_* functions.

        Survival is checked at the file level (valid JSON, no torn/truncated
        write) rather than per-thread value equality, since threads targeting
        the same key legitimately race — last-write-wins per key is expected
        and acceptable under N-way contention.
        """
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def worker(i: int) -> None:
            barrier.wait()
            try:
                if i % 3 == 0:
                    dt = datetime.now(UTC) + timedelta(hours=1, seconds=i)
                    save_usage_limited_until(dt)
                elif i % 3 == 1:
                    save_availability_probe_cache(
                        AvailabilityProbeCache(
                            probed_at=datetime.now(UTC),
                            available=bool(i % 2),
                            latched=bool((i + 1) % 2),
                        )
                    )
                else:
                    save_main_drift_latches({f"client-{i}": True})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Some threads raised: {errors}"

        import cw.dispatch_state

        raw_text = cw.dispatch_state.DISPATCH_STATE_FILE.read_text()
        raw = json.loads(raw_text)  # proves no torn/truncated write
        assert "usage_limited_until" in raw
        assert "availability_probe" in raw
        assert "main_drift_latches" in raw
