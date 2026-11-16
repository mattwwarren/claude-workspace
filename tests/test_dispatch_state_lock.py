"""Concurrency-safety tests for DISPATCH_STATE_FILE writes (#1256).

Regression tests proving the lost-update race on the shared dispatch-state
sidecar (read-merge-write across ``merge_and_save_usage_limited_until``,
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

import contextlib
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

import cw.dispatch_state
from cw.config import _REAL_STATE_DIR
from cw.dispatch_state import (
    AvailabilityProbeCache,
    ExecutorBlockedMarker,
    OpenPrProbeCache,
    clear_all_executor_blocked_markers,
    clear_executor_blocked_marker,
    load_availability_probe_cache,
    load_executor_blocked_markers,
    load_open_pr_probe_cache,
    load_usage_limited_until,
    merge_and_save_usage_limited_until,
    mutate_dispatch_state,
    save_availability_probe_cache,
    save_executor_blocked_marker,
    save_main_drift_latches,
    save_open_pr_probe_entries,
    save_open_pr_probe_entry,
    save_usage_limit_armed_at,
)
from cw.exceptions import CwError
from tests._shared.lock_helpers import DISPATCH_STATE_LOCK_NAME, record_state_locks
from tests._shared.sidecar_failures import fail_state_writes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


class TestDispatchStateLockConcurrency:
    def test_concurrent_availability_and_usage_limit_writes_both_survive(
        self, tmp_config_dir: Path
    ) -> None:
        """Thread A writes usage_limited_until, thread B writes availability_probe.

        Barrier-synchronized so both threads race on the read-merge-write
        window; both keys must survive (neither write may clobber the
        other's key, per #1157's contract — now additionally serialized by
        dispatch_state_lock()). ``usage_limited_until`` is a per-client
        mapping (#1409).
        """
        barrier = threading.Barrier(2)
        errors: list[Exception] = []
        future = datetime.now(UTC) + timedelta(hours=1)
        probed_at = datetime.now(UTC)

        def write_usage_limit() -> None:
            barrier.wait()
            try:
                merge_and_save_usage_limited_until({"acme": future})
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
        assert set(loaded_limit) == {"acme"}
        assert abs((loaded_limit["acme"] - future).total_seconds()) < 1

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
                merge_and_save_usage_limited_until({"acme": future})
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
        assert set(loaded_limit) == {"acme"}
        assert abs((loaded_limit["acme"] - future).total_seconds()) < 1

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
                merge_and_save_usage_limited_until({"acme": future})
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
        assert set(loaded_limit) == {"acme"}
        assert abs((loaded_limit["acme"] - future).total_seconds()) < 1

        markers = load_executor_blocked_markers()
        assert "acme/1723" in markers
        assert markers["acme/1723"].executor == "codex"

    @pytest.mark.parametrize("n_threads", [5, 10])
    def test_many_concurrent_dispatch_state_writes_all_survive(
        self, tmp_config_dir: Path, n_threads: int
    ) -> None:
        """N threads round-robin across the three save_* functions.

        Survival is checked at the file level (valid JSON, no torn/truncated
        write). The availability-probe and main-drift-latches writers target
        one shared key each, so those legitimately race -- last-write-wins is
        expected and acceptable under N-way contention.

        ``merge_and_save_usage_limited_until`` (#1409 round 5) is a
        read-merge-write per client, and each usage-limit writer here uses
        its own distinct client key, so none of them race each other for the
        same key -- every one of their windows must survive the concurrent
        writes, not just one winner.
        """
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []
        usage_limit_writes: dict[str, datetime] = {}

        def worker(i: int) -> None:
            barrier.wait()
            try:
                if i % 3 == 0:
                    dt = datetime.now(UTC) + timedelta(hours=1, seconds=i)
                    usage_limit_writes[f"client-{i}"] = dt
                    merge_and_save_usage_limited_until({f"client-{i}": dt})
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

        loaded_limit = load_usage_limited_until()
        assert set(loaded_limit) == set(usage_limit_writes)
        for client, expected in usage_limit_writes.items():
            assert abs((loaded_limit[client] - expected).total_seconds()) < 1


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
        assert entry.pr_url is None

    def test_round_trip_with_pr_url(self, tmp_config_dir: Path) -> None:
        """The open PR's URL survives a save/load round trip (#2740)."""
        url = "https://github.com/acme/widgets/pull/42"
        save_open_pr_probe_entry(
            "acme",
            "2740",
            OpenPrProbeCache(datetime.now(UTC), has_open_pr=True, pr_url=url),
        )

        assert load_open_pr_probe_cache()["acme/2740"].pr_url == url

    def test_legacy_entry_without_pr_url_is_kept(self, tmp_config_dir: Path) -> None:
        """A pre-#2740 entry loads with ``pr_url`` None rather than being dropped."""
        import cw.dispatch_state

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps(
                {
                    "open_pr_probe": {
                        "acme/legacy": {
                            "probed_at": datetime.now(UTC).isoformat(),
                            "has_open_pr": True,
                        }
                    }
                }
            )
        )

        entry = load_open_pr_probe_cache()["acme/legacy"]

        assert entry.has_open_pr is True
        assert entry.pr_url is None

    @pytest.mark.parametrize("bad_url", [42, "", ["x"]])
    def test_non_string_pr_url_is_tolerated(
        self, tmp_config_dir: Path, bad_url: object
    ) -> None:
        """An unusable ``pr_url`` keeps the verdict and reads back as None."""
        import cw.dispatch_state

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps(
                {
                    "open_pr_probe": {
                        "acme/odd": {
                            "probed_at": datetime.now(UTC).isoformat(),
                            "has_open_pr": True,
                            "pr_url": bad_url,
                        }
                    }
                }
            )
        )

        entry = load_open_pr_probe_cache()["acme/odd"]

        assert entry.has_open_pr is True
        assert entry.pr_url is None

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
        future = datetime.now(UTC) + timedelta(hours=1)
        merge_and_save_usage_limited_until({"acme": future})

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
        merge_and_save_usage_limited_until({"acme": future})
        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=True, latched=False
            )
        )

        save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(datetime.now(UTC), has_open_pr=True)
        )

        loaded_limit = load_usage_limited_until()
        assert set(loaded_limit) == {"acme"}
        assert abs((loaded_limit["acme"] - future).total_seconds()) < 1
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

        fail_state_writes(monkeypatch)

        save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(datetime.now(UTC), has_open_pr=True)
        )

        assert load_open_pr_probe_cache() == {}


class TestMutateDispatchState:
    """The #3638 shared locked read-merge-write primitive, directly."""

    def test_true_writes_mutated_payload_keeping_siblings(
        self, tmp_config_dir: Path
    ) -> None:
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps({"sibling": {"k": 1}})
        )

        def _set(payload: dict[str, Any]) -> bool:
            payload["new"] = 2
            return True

        assert mutate_dispatch_state(_set) is True
        stored = json.loads(cw.dispatch_state.DISPATCH_STATE_FILE.read_text())
        assert stored == {"sibling": {"k": 1}, "new": 2}

    def test_false_skips_write_and_leaves_absent_file_absent(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts = fail_state_writes(monkeypatch)

        assert mutate_dispatch_state(lambda _p: False) is True
        assert attempts == []
        assert not cw.dispatch_state.DISPATCH_STATE_FILE.exists()

    def test_false_leaves_existing_bytes_unmodified(self, tmp_config_dir: Path) -> None:
        original = '{"a": 1}'
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(original)

        assert mutate_dispatch_state(lambda _p: False) is True
        assert cw.dispatch_state.DISPATCH_STATE_FILE.read_text() == original

    def test_oserror_with_message_logs_exact_text_and_returns_false(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fail_state_writes(monkeypatch)

        def _set(payload: dict[str, Any]) -> bool:
            payload["x"] = 1
            return True

        with caplog.at_level(logging.WARNING, logger="cw.dispatch_state"):
            ok = mutate_dispatch_state(
                _set,
                failure_msg="dispatch_state: failed to %s thing (%s)",
                failure_args=("persist", ["a", "b"]),
            )

        assert ok is False
        records = [r for r in caplog.records if r.name == "cw.dispatch_state"]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.getMessage() == (
            "dispatch_state: failed to persist thing (['a', 'b'])"
        )
        assert isinstance(record.exc_info, tuple)

    def test_exc_info_false_omits_traceback(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fail_state_writes(monkeypatch)

        def _set(payload: dict[str, Any]) -> bool:
            payload["x"] = 1
            return True

        with caplog.at_level(logging.WARNING, logger="cw.dispatch_state"):
            mutate_dispatch_state(_set, failure_msg="plain", exc_info=False)

        record = next(r for r in caplog.records if r.name == "cw.dispatch_state")
        assert record.getMessage() == "plain"
        # logging stores the argument as given (False), not None.
        assert not record.exc_info

    def test_oserror_without_message_propagates(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fail_state_writes(monkeypatch)

        def _set(payload: dict[str, Any]) -> bool:
            payload["x"] = 1
            return True

        with pytest.raises(OSError, match="disk full"):
            mutate_dispatch_state(_set)

    def test_strict_corrupt_file_raises_oserror_without_message(
        self, tmp_config_dir: Path
    ) -> None:
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("{not json")

        with pytest.raises(OSError, match="could not read dispatch state"):
            mutate_dispatch_state(lambda _p: True, strict=True)

    def test_strict_corrupt_file_swallowed_when_message_given(
        self,
        tmp_config_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("{not json")

        with caplog.at_level(logging.WARNING, logger="cw.dispatch_state"):
            ok = mutate_dispatch_state(
                lambda _p: True, failure_msg="strict failed", strict=True
            )

        assert ok is False
        assert cw.dispatch_state.DISPATCH_STATE_FILE.read_text() == "{not json"

    def test_lenient_corrupt_file_hands_empty_dict_to_mutate(
        self, tmp_config_dir: Path
    ) -> None:
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("{not json")
        seen: list[dict[str, Any]] = []

        def _set(payload: dict[str, Any]) -> bool:
            seen.append(dict(payload))
            return False

        assert mutate_dispatch_state(_set) is True
        assert seen == [{}]

    def test_one_lock_acquisition_covers_load_mutate_and_write(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock_held = False
        real_lock = cw.dispatch_state.dispatch_state_lock

        @contextlib.contextmanager
        def tracking_lock() -> Iterator[None]:
            nonlocal lock_held
            with real_lock():
                lock_held = True
                try:
                    yield
                finally:
                    lock_held = False

        held_at: dict[str, bool] = {}
        real_load = cw.dispatch_state._load_dispatch_state_raw
        real_write = cw.dispatch_state.atomic_write_text

        def spying_load(*, strict: bool = False) -> dict[str, Any]:
            held_at["load"] = lock_held
            return real_load(strict=strict)

        def spying_write(path: Path, text: str) -> None:
            held_at["write"] = lock_held
            real_write(path, text)

        monkeypatch.setattr(cw.dispatch_state, "dispatch_state_lock", tracking_lock)
        monkeypatch.setattr(cw.dispatch_state, "_load_dispatch_state_raw", spying_load)
        monkeypatch.setattr(cw.dispatch_state, "atomic_write_text", spying_write)

        def _set(payload: dict[str, Any]) -> bool:
            held_at["mutate"] = lock_held
            payload["x"] = 1
            return True

        with record_state_locks() as recorder:
            assert mutate_dispatch_state(_set) is True

        assert held_at == {"load": True, "mutate": True, "write": True}
        assert recorder.acquires(DISPATCH_STATE_LOCK_NAME) == 1

    def test_cwerror_propagates_and_releases_lock(self, tmp_config_dir: Path) -> None:
        def _refuse(_payload: dict[str, Any]) -> bool:
            msg = "refused by mutate"
            raise CwError(msg)

        with pytest.raises(CwError, match="refused by mutate"):
            mutate_dispatch_state(_refuse, failure_msg="must not swallow")

        assert mutate_dispatch_state(lambda _p: True) is True

    def test_reads_state_file_at_call_time(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alt = tmp_config_dir / "alt" / "s.json"
        monkeypatch.setattr("cw.dispatch_state.DISPATCH_STATE_FILE", alt)

        assert mutate_dispatch_state(lambda p: p.update(a=1) or True) is True
        assert json.loads(alt.read_text()) == {"a": 1}

    def test_real_state_guard_propagates_even_with_failure_msg(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.dispatch_state.DISPATCH_STATE_FILE",
            _REAL_STATE_DIR / "dispatch_state.json",
        )
        attempts = fail_state_writes(monkeypatch)

        with pytest.raises(CwError, match="refusing real-state write"):
            mutate_dispatch_state(lambda _p: True, failure_msg="x")

        assert attempts == []

    def test_oserror_from_mutate_follows_the_failure_policy(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        attempts = fail_state_writes(monkeypatch)

        def _raises(_payload: dict[str, Any]) -> bool:
            msg = "mutate blew up"
            raise OSError(msg)

        with caplog.at_level(logging.WARNING, logger="cw.dispatch_state"):
            assert mutate_dispatch_state(_raises, failure_msg="swallowed") is False
        assert [r.getMessage() for r in caplog.records] == ["swallowed"]

        with pytest.raises(OSError, match="mutate blew up"):
            mutate_dispatch_state(_raises, failure_msg=None)

        assert attempts == []


_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
_SIBLING_SEED: dict[str, Any] = {"sibling": {"k": 1}}


def _blocked_marker() -> ExecutorBlockedMarker:
    return ExecutorBlockedMarker(
        client="client-a",
        ticket_id="1723",
        executor="codex",
        reviewer_role=None,
        started_at=_NOW,
        session_id="sid-1723",
    )


class _FailSite(NamedTuple):
    """One migrated sidecar writer and the warning its failure must log (#3638)."""

    call: Callable[[], object]
    message: str
    exc_info: bool
    seed: dict[str, Any]


_FAIL_SITES = {
    "save_usage_limit_armed_at": _FailSite(
        lambda: save_usage_limit_armed_at(_NOW),
        "dispatch_state: failed to persist usage_limit_armed_at",
        False,
        _SIBLING_SEED,
    ),
    "save_availability_probe_cache": _FailSite(
        lambda: save_availability_probe_cache(
            AvailabilityProbeCache(probed_at=_NOW, available=True, latched=False)
        ),
        "dispatch_state: failed to persist availability_probe",
        False,
        _SIBLING_SEED,
    ),
    "save_main_drift_latches": _FailSite(
        lambda: save_main_drift_latches({"b": True, "a": False}),
        "dispatch_state: failed to persist main_drift_latches (clients=['a', 'b'])",
        True,
        _SIBLING_SEED,
    ),
    "save_executor_blocked_marker": _FailSite(
        lambda: save_executor_blocked_marker(_blocked_marker()),
        "dispatch_state: failed to persist executor_blocked marker (client-a/1723)",
        True,
        _SIBLING_SEED,
    ),
    "clear_executor_blocked_marker": _FailSite(
        lambda: clear_executor_blocked_marker("client-a", "1723"),
        "dispatch_state: failed to clear executor_blocked marker (client-a/1723)",
        True,
        {
            **_SIBLING_SEED,
            "executor_blocked": {"client-a/1723": {"client": "client-a"}},
        },
    ),
    "clear_all_executor_blocked_markers": _FailSite(
        clear_all_executor_blocked_markers,
        "dispatch_state: failed to clear executor_blocked markers at boot",
        True,
        _SIBLING_SEED,
    ),
    "save_open_pr_probe_entry": _FailSite(
        lambda: save_open_pr_probe_entry(
            "acme", "1862", OpenPrProbeCache(probed_at=_NOW, has_open_pr=True)
        ),
        "dispatch_state: failed to persist 1 open_pr_probe entry/entries"
        " (['acme/1862'])",
        True,
        _SIBLING_SEED,
    ),
}


class TestSidecarFailContract:
    """Per-site fail-soft contract of every migrated sidecar writer (#3638).

    Pins what the primitive's callers hand it: the exact formatted warning
    (so swapped ``failure_args`` or a changed format string fail), whether a
    traceback rides along, the ``cw.dispatch_state`` logger at WARNING, and that
    a failed write leaves the sidecar bytes untouched.
    """

    @pytest.mark.parametrize("site", list(_FAIL_SITES))
    def test_failed_write_logs_the_exact_warning_and_leaves_the_file_alone(
        self,
        site: str,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        spec = _FAIL_SITES[site]
        state_file = cw.dispatch_state.DISPATCH_STATE_FILE
        state_file.write_text(json.dumps(spec.seed))
        before = state_file.read_bytes()
        attempts = fail_state_writes(monkeypatch)

        with caplog.at_level(logging.DEBUG, logger="cw.dispatch_state"):
            spec.call()

        assert len(attempts) == 1
        records = [r for r in caplog.records if r.name == "cw.dispatch_state"]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.getMessage() == spec.message
        assert bool(record.exc_info) is spec.exc_info
        assert state_file.read_bytes() == before

    def test_merge_and_save_fallback_returns_windows_and_logs_without_traceback(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        until = _NOW + timedelta(hours=1)
        state_file = cw.dispatch_state.DISPATCH_STATE_FILE
        state_file.write_text(json.dumps(_SIBLING_SEED))
        before = state_file.read_bytes()
        fail_state_writes(monkeypatch)

        with caplog.at_level(logging.DEBUG, logger="cw.dispatch_state"):
            merged = merge_and_save_usage_limited_until(
                {"acme": until}, fresh={"beta": until}, clears_seen={}
            )

        assert merged == {"acme": until, "beta": until}
        records = [r for r in caplog.records if r.name == "cw.dispatch_state"]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert (
            records[0].getMessage()
            == "dispatch_state: failed to persist usage_limited_until"
        )
        assert not records[0].exc_info
        assert state_file.read_bytes() == before


class TestSidecarWriteShapes:
    """Persisted shapes the writer migration must keep byte-for-byte (#3638)."""

    def test_clear_marker_on_an_absent_file_creates_nothing(
        self, tmp_config_dir: Path
    ) -> None:
        clear_executor_blocked_marker("client-a", "1723")

        assert not cw.dispatch_state.DISPATCH_STATE_FILE.exists()

    def test_clear_marker_with_a_non_dict_value_writes_nothing(
        self, tmp_config_dir: Path
    ) -> None:
        state_file = cw.dispatch_state.DISPATCH_STATE_FILE
        state_file.write_text('{"executor_blocked": ["not", "a", "dict"]}')
        before = state_file.read_bytes()

        clear_executor_blocked_marker("client-a", "1723")

        assert state_file.read_bytes() == before

    def test_arming_none_persists_json_null(self, tmp_config_dir: Path) -> None:
        save_usage_limit_armed_at(None)

        stored = json.loads(cw.dispatch_state.DISPATCH_STATE_FILE.read_text())
        assert stored == {"usage_limit_armed_at": None}

    def test_clear_all_markers_persists_an_empty_dict(
        self, tmp_config_dir: Path
    ) -> None:
        save_executor_blocked_marker(_blocked_marker())

        clear_all_executor_blocked_markers()

        stored = json.loads(cw.dispatch_state.DISPATCH_STATE_FILE.read_text())
        assert stored == {"executor_blocked": {}}


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
        merge_and_save_usage_limited_until({"acme": future})

        save_open_pr_probe_entries(
            "acme",
            {"1862": OpenPrProbeCache(datetime.now(UTC), has_open_pr=True)},
        )

        loaded_limit = load_usage_limited_until()
        assert set(loaded_limit) == {"acme"}
        assert abs((loaded_limit["acme"] - future).total_seconds()) < 1
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


class TestMergeAndSaveClearMarkFilter:
    """``merge_and_save_usage_limited_until``'s ``fresh`` / ``clears_seen`` (#3547)."""

    def test_carried_entry_with_unseen_mark_is_not_persisted(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.dispatch_state_marks import clear_usage_limited_until

        now = datetime.now(UTC)
        until = now + timedelta(hours=1)
        clear_usage_limited_until("acme", cleared_at=now)

        with caplog.at_level("INFO", logger="cw.dispatch_state"):
            merged = merge_and_save_usage_limited_until(
                {"acme": until, "beta": until}, clears_seen={}
            )

        assert merged == {"beta": until}
        assert load_usage_limited_until() == {"beta": until}
        assert any(
            "dispatch_state: dropped carried usage_limited_until for" in r.getMessage()
            and "acme" in r.getMessage()
            for r in caplog.records
        )

    def test_fresh_entry_for_an_unseen_mark_client_is_persisted(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.dispatch_state_marks import clear_usage_limited_until

        now = datetime.now(UTC)
        stale = now + timedelta(hours=3)
        fresh = now + timedelta(hours=1)
        clear_usage_limited_until("acme", cleared_at=now)

        merged = merge_and_save_usage_limited_until(
            {"acme": stale}, fresh={"acme": fresh}, clears_seen={}
        )

        assert merged == {"acme": fresh}
        assert load_usage_limited_until() == {"acme": fresh}

    def test_seen_mark_and_absent_mark_are_retained(self, tmp_config_dir: Path) -> None:
        from cw.dispatch_state_marks import clear_usage_limited_until

        now = datetime.now(UTC)
        until = now + timedelta(hours=1)
        clear_usage_limited_until("acme", cleared_at=now)

        merged = merge_and_save_usage_limited_until(
            {"acme": until, "beta": until}, clears_seen={"acme": now, "beta": now}
        )

        assert merged == {"acme": until, "beta": until}
        assert load_usage_limited_until() == {"acme": until, "beta": until}

    def test_os_error_fallback_returns_windows_merged_with_fresh(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        until = now + timedelta(hours=1)

        fail_state_writes(monkeypatch)

        merged = merge_and_save_usage_limited_until(
            {"acme": until}, fresh={"beta": until}, clears_seen={}
        )

        assert merged == {"acme": until, "beta": until}

    def test_defaults_keep_todays_behaviour(self, tmp_config_dir: Path) -> None:
        from cw.dispatch_state_marks import clear_usage_limited_until

        now = datetime.now(UTC)
        until = now + timedelta(hours=1)
        clear_usage_limited_until("acme", cleared_at=now)

        assert merge_and_save_usage_limited_until({"acme": until}) == {"acme": until}
        assert load_usage_limited_until() == {"acme": until}
