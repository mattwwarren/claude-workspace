"""Tests for ``cw.focus`` — the session-scoped focus pointer store (#1644).

Covers the fail-safe loader (R3), the set/get/clear round-trip, R6's
no-expiry/no-pruning contract (``clear_focus`` is the only deletion path), and
the dedicated file lock mirrored from ``cw.dev_queue.storage._lock`` (R4).
"""

from __future__ import annotations

import threading
from pathlib import Path

from cw.config import focus_file, focus_lock_file
from cw.focus import (
    clear_focus,
    focus_lock,
    get_focus,
    load_focus_store,
    save_focus_store,
    set_focus,
)
from cw.models import FocusEntry


class TestLoadFocusStore:
    def test_absent_file_returns_empty_dict(self, tmp_config_dir: Path) -> None:
        assert not focus_file().exists()
        assert load_focus_store() == {}

    def test_malformed_json_returns_empty_dict(self, tmp_config_dir: Path) -> None:
        """R3: a corrupt focus.json must degrade, never raise."""
        path = focus_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json at all")

        assert load_focus_store() == {}

    def test_schema_invalid_payload_returns_empty_dict(
        self, tmp_config_dir: Path
    ) -> None:
        """Well-formed JSON that is not a session->FocusEntry map degrades too."""
        path = focus_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"sess-1": {"lane": "impl"}}')

        assert load_focus_store() == {}

    def test_non_object_json_returns_empty_dict(self, tmp_config_dir: Path) -> None:
        path = focus_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]")

        assert load_focus_store() == {}


class TestSetGetRoundTrip:
    def test_client_only_round_trip(self, tmp_config_dir: Path) -> None:
        set_focus("sess-1", "client-a")

        entry = get_focus("sess-1")
        assert entry is not None
        assert entry.client == "client-a"
        assert entry.lane is None

    def test_client_and_lane_round_trip(self, tmp_config_dir: Path) -> None:
        set_focus("sess-1", "client-a", "impl")

        entry = get_focus("sess-1")
        assert entry is not None
        assert entry.client == "client-a"
        assert entry.lane == "impl"

    def test_set_focus_overwrites_prior_entry(self, tmp_config_dir: Path) -> None:
        set_focus("sess-1", "client-a", "impl")
        set_focus("sess-1", "client-b")

        entry = get_focus("sess-1")
        assert entry is not None
        assert entry.client == "client-b"
        assert entry.lane is None

    def test_get_focus_unknown_session_returns_none(self, tmp_config_dir: Path) -> None:
        assert get_focus("never-set") is None

    def test_set_focus_stamps_set_at(self, tmp_config_dir: Path) -> None:
        entry = set_focus("sess-1", "client-a")

        assert entry.set_at.tzinfo is not None

    def test_other_sessions_survive_a_set(self, tmp_config_dir: Path) -> None:
        set_focus("sess-1", "client-a")
        set_focus("sess-2", "client-b", "debt")

        assert get_focus("sess-1") is not None
        entry = get_focus("sess-2")
        assert entry is not None
        assert entry.lane == "debt"

    def test_save_focus_store_round_trips_through_disk(
        self, tmp_config_dir: Path
    ) -> None:
        save_focus_store({"sess-9": FocusEntry(client="client-z", lane="review")})

        loaded = load_focus_store()
        assert set(loaded) == {"sess-9"}
        assert loaded["sess-9"].client == "client-z"
        assert loaded["sess-9"].lane == "review"


class TestClearFocus:
    def test_clear_removes_entry(self, tmp_config_dir: Path) -> None:
        set_focus("sess-1", "client-a", "impl")

        clear_focus("sess-1")

        assert get_focus("sess-1") is None

    def test_clear_is_idempotent_on_unknown_session(self, tmp_config_dir: Path) -> None:
        clear_focus("never-set")
        clear_focus("never-set")

        assert get_focus("never-set") is None

    def test_clear_leaves_other_sessions_untouched(self, tmp_config_dir: Path) -> None:
        """R6: no pruning — clear_focus is the ONLY deletion path, and it is
        scoped to a single session id."""
        set_focus("sess-1", "client-a")
        set_focus("sess-2", "client-b")

        clear_focus("sess-1")

        assert get_focus("sess-1") is None
        assert get_focus("sess-2") is not None


class TestFocusLock:
    def test_lock_uses_its_own_dedicated_file(self, tmp_config_dir: Path) -> None:
        """R4: the focus lock is a dedicated sibling file, isolated from the
        dev-queue and concurrency-override locks."""
        import cw.config as cfg

        lock_path = focus_lock_file()
        assert lock_path != cfg.DEV_QUEUE_LOCK
        assert lock_path != cfg.CONCURRENCY_OVERRIDE_LOCK
        assert lock_path != focus_file()
        assert lock_path.parent == cfg.STATE_DIR

    def test_lock_creates_its_file_under_tmp(self, tmp_config_dir: Path) -> None:
        with focus_lock():
            pass

        assert focus_lock_file().exists()
        assert str(tmp_config_dir) in str(focus_lock_file())

    def test_concurrent_sets_for_distinct_sessions_both_survive(
        self, tmp_config_dir: Path
    ) -> None:
        """Mirrors tests/test_clients_lock.py — no lost update under the lock."""
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def do_set(session_id: str, client: str) -> None:
            barrier.wait()
            try:
                set_focus(session_id, client)
            except Exception as exc:  # noqa: BLE001 — surfaced via assert below
                errors.append(exc)

        t1 = threading.Thread(target=do_set, args=("sess-1", "client-a"))
        t2 = threading.Thread(target=do_set, args=("sess-2", "client-b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"set_focus raised: {errors}"
        store = load_focus_store()
        assert "sess-1" in store, "sess-1 was lost"
        assert "sess-2" in store, "sess-2 was lost"
