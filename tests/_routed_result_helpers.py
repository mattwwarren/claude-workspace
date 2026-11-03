"""Shared world builders for the routed-orphan close tests (#2517, #2708).

Moved out of ``tests/test_cli_routed_orphan_close.py`` so the approve/requeue
tests and the named ``cw doctor --reap --routed-session-id`` tests seed the
same world. They cannot live in ``tests/conftest.py``: ``_seed_orphans``
needs ``_mk_routed_session`` from ``tests/_reconcile_helpers.py``, which
imports ``tests.conftest`` (an import cycle).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.dev_queue import save_dev_queue
from cw.models import CwState, DevQueueStore, QueueItemStatus, Session, SessionStatus
from tests._reconcile_helpers import _mk_routed_session
from tests.conftest import _make_daemon_session, _make_ticket_task

if TYPE_CHECKING:
    import logging
    from pathlib import Path

    import pytest

    from cw.native_daemon import FakeNativeDaemonClient

_TICKET = "2517"
_CLIENT = "client-a"
_NAME = f"{_CLIENT}/auto-dev/{_TICKET}"


def _seed_orphans(
    tmp_path: Path,
    daemon: FakeNativeDaemonClient,
    *sids: str,
    live: bool = True,
    extra: tuple[Session, ...] = (),
) -> list[Session]:
    """Persist marker sessions for the #2517 ticket (plus *extra*); return them.

    Hermetic (N5): each worktree lives under *tmp_path*, and a live session's
    surface is the short id ``seed_live_worker(worktree)`` registered.
    """
    sessions: list[Session] = []
    for sid in sids or ("orphan-1",):
        worktree = tmp_path / f"wt-{sid}"
        surface = daemon.seed_live_worker(worktree) if live else f"gone-{sid}"
        sess = _mk_routed_session(sid, worktree, surface_ref=surface)
        sess.name = _NAME
        sessions.append(sess)
    save_state(CwState(sessions=[*sessions, *extra]))
    return sessions


def _bystander(tmp_path: Path, *, surface_ref: str = "bystander") -> Session:
    """A live DAEMON session for another ticket, not a #2517 candidate."""
    return _make_daemon_session(
        id="bystander",
        name=f"{_CLIENT}/auto-dev/9999",
        client=_CLIENT,
        worktree_path=tmp_path / "wt-bystander",
        surface_ref=surface_ref,
    )


def _seed_row(
    session_id: str,
    *,
    status: QueueItemStatus = QueueItemStatus.RUNNING,
    ticket_id: str = _TICKET,
) -> None:
    """Persist a single queue row bound to *session_id*."""
    row = _make_ticket_task(
        ticket_id=ticket_id, client=_CLIENT, status=status, session_id=session_id
    )
    save_dev_queue(DevQueueStore(tasks=[row]))


def _status_of(session_id: str) -> SessionStatus:
    return next(s for s in load_state().sessions if s.id == session_id).status


def _records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(f"{event}:")]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _wipe_roster_on_stop(
    monkeypatch: pytest.MonkeyPatch, daemon: FakeNativeDaemonClient
) -> None:
    """A stop that empties the whole roster (a daemon restart mid-stop)."""

    def _wipe(short_id: str) -> None:
        daemon.stop_calls.append(short_id)
        daemon._live.clear()

    monkeypatch.setattr(daemon, "stop", _wipe)
