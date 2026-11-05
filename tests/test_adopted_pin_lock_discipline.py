"""ADR-0019 conformance for the adopted-pin session-end re-pin (RFC 0016 A2, #2943).

The session-end hook is a pure stamp in ``transition_task_status``; the gh and
git work happens lock-free in ``settle_adopted_pins`` on the next tick. These
tests route an adopted row through the real session-end paths that hold
``sessions_lock`` (``cw spawn close`` and the shared ``_apply_sentinel_to_task``
the Stop hook and reconcile sweeps call), and the consume path under
``dev_queue_lock`` only, with every adoption gh/git seam replaced by a stub
that fails the test. The autouse lock-invariant harness additionally fails
any real subprocess under ``sessions_lock`` (its allowlist stays empty).
Then the settle and the requeue re-pin record which locks their I/O ran under.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw._lock_guard import held_locks
from cw.config import load_state, save_state, sessions_lock
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.dispatch import apply_staged_decision
from cw.dispatch.adopted_head_gate import settle_adopted_pins
from cw.exceptions import DEV_QUEUE_LOCK_NAME, SESSIONS_LOCK_NAME
from cw.models import (
    ClientConfig,
    DevQueueStore,
    LastResultSource,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile._shared._routing import _apply_sentinel_to_task
from cw.result import reconstruct_staged_sentinel
from tests._adopt_pr_helpers import ADOPT_PIN, make_adopted_task
from tests._clients_yaml import write_clients_yaml
from tests._reconcile_helpers import _stage_complete_payload
from tests.conftest import _seed_daemon_session

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session

_CLIENT = "test-client"
_TICKET = "ADOPT-1"
_SID = "dead1234"
_DESCENDANT = "d" * 40
_IO_SEAMS = (
    "cw.gh.branch_head_sha_on_origin",
    "cw.dev_queue.adoption.branch_head_sha_on_origin",
    "cw.dev_queue.adoption.is_ancestor",
    "cw.dispatch.adopted_head_gate.branch_head_sha_on_origin",
    "cw.dispatch.adopted_head_gate.is_ancestor",
    "cw.dispatch.adopted_head_gate.fetch_feature_branch",
)


def _forbid_adoption_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_a: object, **_kw: object) -> object:
        msg = "an adoption gh/git call ran on a session-end path"
        raise AssertionError(msg)

    for seam in _IO_SEAMS:
        monkeypatch.setattr(seam, _forbidden)


@pytest.fixture
def client(tmp_path: Path) -> ClientConfig:
    cfg = ClientConfig(
        name=_CLIENT, workspace_path=tmp_path / "ws", default_branch="main"
    )
    write_clients_yaml(cfg, ensure_workspaces=True)
    return cfg


def _seed_row(stage: Stage = Stage.IMPL) -> TicketTask:
    task = make_adopted_task(
        ticket_id=_TICKET,
        client=_CLIENT,
        status=QueueItemStatus.RUNNING,
        session_id=_SID,
        stage=stage,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    return task


def _seed_staged_session(tmp_path: Path, tmp_config_dir: Path) -> Session:
    sess = _seed_daemon_session(
        tmp_path,
        tmp_config_dir,
        session_id=_SID,
        name=f"{_CLIENT}/auto-dev/{_TICKET}",
        surface_ref="abc12345",
    )
    state = load_state()
    stored = state.find_by_name_or_id(sess.id)
    assert stored is not None
    stored.last_result = _stage_complete_payload()
    stored.last_result_source = LastResultSource.EMIT_CLI
    save_state(state)
    return stored


def _row() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == _TICKET)


def _assert_stamped_not_moved() -> None:
    row = _row()
    assert row.status != QueueItemStatus.RUNNING
    assert row.adopted_repin_due is True
    assert row.adopted_head_sha == ADOPT_PIN


def _settle_outside_every_lock(
    client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []

    def _head(_branch: str, **_kw: object) -> tuple[str | None, bool]:
        seen.append(tuple(h.name for h in held_locks()))
        return _DESCENDANT, True

    def _ancestor(*_a: object, **_kw: object) -> bool:
        seen.append(tuple(h.name for h in held_locks()))
        return True

    monkeypatch.setattr(
        "cw.dispatch.adopted_head_gate.branch_head_sha_on_origin", _head
    )
    monkeypatch.setattr("cw.dispatch.adopted_head_gate.is_ancestor", _ancestor)
    monkeypatch.setattr(
        "cw.dispatch.adopted_head_gate.fetch_adopted_branch", lambda *_a: None
    )
    settle_adopted_pins(client)
    assert seen
    assert all(names == () for names in seen)
    row = _row()
    assert row.adopted_head_sha == _DESCENDANT
    assert row.adopted_repin_due is False


def test_spawn_close_path_stamps_without_io(
    client: ClientConfig,
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.cli.spawn import _spawn_close_impl

    _seed_staged_session(tmp_path, tmp_config_dir)
    _seed_row()
    _forbid_adoption_io(monkeypatch)

    _spawn_close_impl(session_id=_SID, native_daemon=FakeNativeDaemonClient())

    assert _row().stage == Stage.REVIEW
    _assert_stamped_not_moved()
    _settle_outside_every_lock(client, monkeypatch)


def test_shared_sentinel_router_under_sessions_lock_stamps_without_io(
    client: ClientConfig,
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Stop hook and the reconcile sweeps route through this under sessions_lock."""
    sess = _seed_staged_session(tmp_path, tmp_config_dir)
    _seed_row()
    _forbid_adoption_io(monkeypatch)
    sentinel = reconstruct_staged_sentinel(sess.last_result)
    assert sentinel is not None

    with sessions_lock():
        outcome = _apply_sentinel_to_task(_TICKET, sess, sentinel)

    assert outcome.routed
    _assert_stamped_not_moved()
    _settle_outside_every_lock(client, monkeypatch)


def test_consume_path_under_dev_queue_lock_stamps_without_io(
    client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_row()
    _forbid_adoption_io(monkeypatch)

    with dev_queue_lock():
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == _TICKET)
        apply_staged_decision(task, "stage_complete", None, {_CLIENT: client})
        save_dev_queue(store)

    _assert_stamped_not_moved()
    _settle_outside_every_lock(client, monkeypatch)


def test_requeue_repin_io_runs_under_dev_queue_lock_never_sessions_lock(
    client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw.dev_queue import ADOPTED_HEAD_DRIFT_DISPOSITION
    from cw.dev_queue.requeue import requeue_ticket

    task = make_adopted_task(
        ticket_id=_TICKET,
        client=_CLIENT,
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition=ADOPTED_HEAD_DRIFT_DISPOSITION,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    seen: list[tuple[str, ...]] = []

    def _head(_branch: str, **_kw: object) -> tuple[str | None, bool]:
        seen.append(tuple(h.name for h in held_locks()))
        return _DESCENDANT, True

    def _ancestor(*_a: object, **_kw: object) -> bool:
        seen.append(tuple(h.name for h in held_locks()))
        return True

    monkeypatch.setattr("cw.dev_queue.adoption.branch_head_sha_on_origin", _head)
    monkeypatch.setattr("cw.dev_queue.adoption.is_ancestor", _ancestor)
    monkeypatch.setattr(
        "cw.dev_queue.requeue.get_native_daemon_client", FakeNativeDaemonClient
    )

    requeue_ticket(_TICKET, _CLIENT, repin_adopted_head=True)

    assert len(seen) == 2
    for names in seen:
        assert DEV_QUEUE_LOCK_NAME in names
        assert SESSIONS_LOCK_NAME not in names
    assert _row().adopted_head_sha == _DESCENDANT
