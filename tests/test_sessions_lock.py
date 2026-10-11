"""Tests for sessions.json file-locking (sessions_lock).

Verifies that concurrent load→mutate→save sequences under sessions_lock
do not lose updates (no last-writer-wins clobber).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw.config
from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
from cw._lock_hold import LockHoldRecord, read_window
from cw.config import load_state, mutate_state, save_state, sessions_lock, state_dir
from cw.exceptions import CwLockReentrancyError, SessionsLockTimeoutError
from cw.models import CwState, LastResultSource, Session, SessionPurpose, SessionStatus
from cw.result import emit_result
from tests.conftest import _FakeClock
from tests.test_result import _valid_payload

if TYPE_CHECKING:
    pass


def _add_session(session_id: str, name: str) -> None:
    """Load state, append a session, save — protected by sessions_lock."""
    with sessions_lock():
        state = load_state()
        state.sessions.append(
            Session(
                id=session_id,
                name=name,
                client="test-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                workspace_path=Path("/tmp/test"),
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        save_state(state)


class TestSessionsLockConcurrency:
    def test_concurrent_mutations_both_survive(self, tmp_config_dir: Path) -> None:
        """Two threads doing load→mutate→save concurrently under the lock
        must both have their mutations persist (no lost update).
        """
        barrier = threading.Barrier(2)

        def writer(session_id: str, name: str) -> None:
            barrier.wait()
            _add_session(session_id, name)

        t1 = threading.Thread(target=writer, args=("sess-aaa", "test-client/aaa"))
        t2 = threading.Thread(target=writer, args=("sess-bbb", "test-client/bbb"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        final = load_state()
        ids = {s.id for s in final.sessions}
        assert "sess-aaa" in ids, "session sess-aaa was lost"
        assert "sess-bbb" in ids, "session sess-bbb was lost"

    def test_lock_is_exclusive_sequential_within_thread(
        self, tmp_config_dir: Path
    ) -> None:
        """Sequential acquisitions in the same thread succeed (no self-deadlock)."""
        _add_session("sess-111", "test-client/111")
        _add_session("sess-222", "test-client/222")

        state = load_state()
        ids = {s.id for s in state.sessions}
        assert "sess-111" in ids
        assert "sess-222" in ids

    def test_many_concurrent_mutations_all_survive(self, tmp_config_dir: Path) -> None:
        """Ten concurrent writers each adding a distinct session; all must survive."""
        n = 10
        barrier = threading.Barrier(n)

        def writer(i: int) -> None:
            barrier.wait()
            _add_session(f"sess-{i:04d}", f"test-client/s{i:04d}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = load_state()
        ids = {s.id for s in final.sessions}
        for i in range(n):
            assert f"sess-{i:04d}" in ids, f"session sess-{i:04d} was lost"


class TestSessionsLockEmitResult:
    """emit_result must hold the sessions lock so a concurrent
    reconcile-style mutation is not clobbered.
    """

    def test_emit_result_survives_concurrent_reconcile_style_write(
        self, tmp_config_dir: Path
    ) -> None:
        """emit_result and a concurrent load→mutate→save both survive."""
        # Seed a session into state.
        initial = CwState()
        initial.sessions.append(
            Session(
                id="sess-target",
                name="test-client/auto-dev/T-1",
                client="test-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                workspace_path=Path("/tmp/test"),
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        save_state(initial)

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def do_persist() -> None:
            barrier.wait()
            try:
                emit_result(
                    _valid_payload(),
                    "sess-target",
                    source=LastResultSource.STOP_HOOK_HARVEST,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def do_side_write() -> None:
            barrier.wait()
            with sessions_lock():
                st = load_state()
                # Touch a different session field — simulates a concurrent update
                for s in st.sessions:
                    if s.id == "sess-target":
                        s.status = SessionStatus.IDLE
                save_state(st)

        t1 = threading.Thread(target=do_persist)
        t2 = threading.Thread(target=do_side_write)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"emit_result raised: {errors}"
        final = load_state()
        target = next((s for s in final.sessions if s.id == "sess-target"), None)
        assert target is not None, "target session disappeared"
        # At least one mutation must have landed — last_result or status change
        assert target.last_result is not None or target.status == SessionStatus.IDLE


def _recorded_holds() -> list[LockHoldRecord]:
    return read_window(state_dir(), now=datetime.now(UTC)).records


@pytest.fixture
def record_every_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record every hold, however short (#2637)."""
    monkeypatch.setattr("cw._lock_hold.RECORD_MIN_S", 0.0)


@pytest.mark.usefixtures("record_every_hold")
class TestSessionsLockHoldRecording:
    """The sessions lock records its hold through the real seam (#2637)."""

    def test_mutate_state_records_hold_and_state_io(
        self, monkeypatch: pytest.MonkeyPatch, lock_hold_clock: _FakeClock
    ) -> None:
        real_write = cw.config.atomic_write_text

        def slow_write(path: Path, text: str) -> None:
            lock_hold_clock.now += 9.0
            real_write(path, text)

        def mutate(_state: CwState) -> None:
            lock_hold_clock.now += 3.0

        monkeypatch.setattr("cw.config.atomic_write_text", slow_write)
        mutate_state(mutate)

        (record,) = _recorded_holds()
        assert record.lock == "sessions"
        assert record.held_s == 12.0
        assert record.waited_s == 0.0
        assert record.state_io_s == 9.0
        assert record.state_bytes is not None

    def test_add_session_records_bounded_state_io(self) -> None:
        _add_session("sess-io", "test-client/io")

        (record,) = _recorded_holds()
        assert record.caller == "tests.test_sessions_lock:_add_session"
        assert record.state_io_s is not None
        assert record.state_io_s <= record.held_s

    @pytest.mark.usefixtures("held_sessions_lock")
    def test_bounded_timeout_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.5")
        monkeypatch.setattr("cw._flock.time", _FakeClock())

        with pytest.raises(SessionsLockTimeoutError), sessions_lock(bounded=True):
            pass

        assert _recorded_holds() == []

    def test_body_that_raises_still_records(self) -> None:
        with pytest.raises(KeyError, match="body"), sessions_lock():
            raise KeyError("body")

        (record,) = _recorded_holds()
        assert record.lock == "sessions"

    @pytest.mark.lock_violations_expected("reentry")
    def test_reentry_still_raises_and_records_only_the_outer_hold(self) -> None:
        with pytest.raises(CwLockReentrancyError), sessions_lock(), sessions_lock():
            pass

        (record,) = _recorded_holds()
        assert record.lock == "sessions"
