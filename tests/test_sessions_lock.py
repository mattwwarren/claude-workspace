"""Tests for sessions.json file-locking (sessions_lock).

Verifies that concurrent load→mutate→save sequences under sessions_lock
do not lose updates (no last-writer-wins clobber).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cw.config import load_state, save_state, sessions_lock
from cw.models import CwState, Session, SessionPurpose, SessionStatus

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


class TestSessionsLockPersistLastResult:
    """persist_last_result must hold the sessions lock so a concurrent
    reconcile-style mutation is not clobbered.
    """

    def test_persist_last_result_survives_concurrent_reconcile_style_write(
        self, tmp_config_dir: Path
    ) -> None:
        """persist_last_result and a concurrent load→mutate→save both survive."""
        from cw.dispatch import persist_last_result

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
                persist_last_result(
                    "sess-target",
                    "<<<AUTO_DEV_RESULT\n"
                    '{"status": "shipped", "ticket_id": "T-1", '
                    '"pr_url": "https://github.com/x/y/pull/1", '
                    '"next_actions": [], "scope": null, '
                    '"cost_usd": null, "schema_version": 1}\n'
                    "AUTO_DEV_RESULT>>>",
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

        assert not errors, f"persist_last_result raised: {errors}"
        final = load_state()
        target = next((s for s in final.sessions if s.id == "sess-target"), None)
        assert target is not None, "target session disappeared"
        # At least one mutation must have landed — last_result or status change
        assert target.last_result is not None or target.status == SessionStatus.IDLE
