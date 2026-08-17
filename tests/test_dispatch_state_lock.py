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
    OpenPrProbeCache,
    load_availability_probe_cache,
    load_executor_blocked_markers,
    load_open_pr_probe_cache,
    load_usage_limited_until,
    save_availability_probe_cache,
    save_executor_blocked_marker,
    save_main_drift_latches,
    save_open_pr_probe_entries,
    save_open_pr_probe_entry,
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


class TestOpenPrProbeCacheSidecar:
    """Round-trip + fail-soft contract for the #1862 per-ticket probe cache."""

    def test_round_trip(self, tmp_config_dir: Path) -> None:
        probed_at = datetime.now(UTC)
        save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(probed_at=probed_at, has_open_pr=True)
        )

        cache = load_open_pr_probe_cache()

        assert "acme/1862" in cache
        entry = cache["acme/1862"]
        assert entry.has_open_pr is True
        assert abs((entry.probed_at - probed_at).total_seconds()) < 1

    def test_entries_for_multiple_tickets_coexist(self, tmp_config_dir: Path) -> None:
        now = datetime.now(UTC)
        save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(probed_at=now, has_open_pr=True)
        )
        save_open_pr_probe_entry(
            "acme", "1863", OpenPrProbeCache(probed_at=now, has_open_pr=False)
        )
        save_open_pr_probe_entry(
            "other", "1862", OpenPrProbeCache(probed_at=now, has_open_pr=True)
        )

        cache = load_open_pr_probe_cache()

        assert set(cache) == {"acme/1862", "acme/1863", "other/1862"}
        assert cache["acme/1863"].has_open_pr is False

    def test_absent_file_returns_empty(self, tmp_config_dir: Path) -> None:
        assert load_open_pr_probe_cache() == {}

    def test_missing_key_returns_empty(self, tmp_config_dir: Path) -> None:
        save_usage_limited_until(datetime.now(UTC) + timedelta(hours=1))

        assert load_open_pr_probe_cache() == {}

    def test_corrupt_file_returns_empty(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("{not json")

        assert load_open_pr_probe_cache() == {}

    def test_malformed_entry_is_dropped_not_poisoning_siblings(
        self, tmp_config_dir: Path
    ) -> None:
        """Per-entry validation, mirroring load_executor_blocked_markers (#1742)."""
        import cw.dispatch_state

        save_open_pr_probe_entry(
            "acme", "good", OpenPrProbeCache(datetime.now(UTC), has_open_pr=True)
        )
        payload = json.loads(cw.dispatch_state.DISPATCH_STATE_FILE.read_text())
        payload["open_pr_probe"]["acme/bad-shape"] = {"probed_at": 42}
        payload["open_pr_probe"]["acme/bad-ts"] = {
            "probed_at": "not-a-timestamp",
            "has_open_pr": True,
        }
        payload["open_pr_probe"]["acme/not-a-dict"] = "nope"
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(json.dumps(payload))

        cache = load_open_pr_probe_cache()

        assert set(cache) == {"acme/good"}

    def test_non_dict_entries_value_returns_empty(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps({"open_pr_probe": ["not", "a", "dict"]})
        )

        assert load_open_pr_probe_cache() == {}

    def test_write_preserves_sibling_keys(self, tmp_config_dir: Path) -> None:
        """Read-merge-write, per the #1157 shared-sidecar contract."""
        future = datetime.now(UTC) + timedelta(hours=1)
        save_usage_limited_until(future)
        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=True, latched=False
            )
        )

        save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(datetime.now(UTC), has_open_pr=True)
        )

        loaded_limit = load_usage_limited_until()
        assert loaded_limit is not None
        assert abs((loaded_limit - future).total_seconds()) < 1
        assert load_availability_probe_cache() is not None
        assert "acme/1862" in load_open_pr_probe_cache()

    def test_re_saving_same_key_overwrites_in_place(self, tmp_config_dir: Path) -> None:
        now = datetime.now(UTC)
        save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(probed_at=now, has_open_pr=True)
        )
        save_open_pr_probe_entry(
            "acme",
            "1862",
            OpenPrProbeCache(probed_at=now + timedelta(seconds=5), has_open_pr=False),
        )

        cache = load_open_pr_probe_cache()

        assert len(cache) == 1
        assert cache["acme/1862"].has_open_pr is False

    def test_non_dict_top_level_json_returns_empty(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps(["not", "a", "dict"])
        )

        assert load_open_pr_probe_cache() == {}

    def test_write_failure_is_swallowed(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-soft persistence: a failed write costs one extra probe, not a raise."""

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.dispatch_state.atomic_write_text", _boom)

        save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(datetime.now(UTC), has_open_pr=True)
        )

        assert load_open_pr_probe_cache() == {}


class TestOpenPrProbeEntriesBatch:
    """The #1862 perf follow-up: batched multi-entry writer.

    ``save_open_pr_probe_entries`` and ``save_open_pr_probe_entry`` share the
    same underlying read-merge-write helper -- these tests pin the batch
    entry point's own contract (one lock window for N entries, no-op on
    empty, preserves sibling keys).
    """

    def test_round_trip_multiple_entries_in_one_call(
        self, tmp_config_dir: Path
    ) -> None:
        now = datetime.now(UTC)
        save_open_pr_probe_entries(
            "acme",
            {
                "1862": OpenPrProbeCache(probed_at=now, has_open_pr=True),
                "1863": OpenPrProbeCache(probed_at=now, has_open_pr=False),
            },
        )

        cache = load_open_pr_probe_cache()

        assert set(cache) == {"acme/1862", "acme/1863"}
        assert cache["acme/1862"].has_open_pr is True
        assert cache["acme/1863"].has_open_pr is False

    def test_empty_dict_is_a_noop_and_acquires_no_lock(
        self, tmp_config_dir: Path
    ) -> None:
        """An empty batch must not create the sidecar file at all."""
        import cw.dispatch_state

        save_open_pr_probe_entries("acme", {})

        assert not cw.dispatch_state.DISPATCH_STATE_FILE.exists()

    def test_batch_write_preserves_sibling_keys(self, tmp_config_dir: Path) -> None:
        """Read-merge-write, per the #1157 shared-sidecar contract."""
        future = datetime.now(UTC) + timedelta(hours=1)
        save_usage_limited_until(future)

        save_open_pr_probe_entries(
            "acme",
            {"1862": OpenPrProbeCache(datetime.now(UTC), has_open_pr=True)},
        )

        loaded_limit = load_usage_limited_until()
        assert loaded_limit is not None
        assert abs((loaded_limit - future).total_seconds()) < 1
        assert "acme/1862" in load_open_pr_probe_cache()

    def test_single_entry_helper_and_batch_helper_agree(
        self, tmp_config_dir: Path
    ) -> None:
        """save_open_pr_probe_entry and save_open_pr_probe_entries write the
        identical on-disk shape (they share one implementation)."""
        now = datetime.now(UTC)
        save_open_pr_probe_entry(
            "acme", "single", OpenPrProbeCache(probed_at=now, has_open_pr=True)
        )
        save_open_pr_probe_entries(
            "acme", {"batch": OpenPrProbeCache(probed_at=now, has_open_pr=True)}
        )

        cache = load_open_pr_probe_cache()
        assert set(cache) == {"acme/single", "acme/batch"}
