"""Bounded ``dev_queue`` lock across the operator entry points (GitHub #2501).

Cross-module contract file (the ``test_sessions_lock.py`` precedent): the
library functions in ``cw.dev_queue.{crud,approval,must_fix_override,requeue}``
forward an opt-in ``bounded`` to the dev-queue ``_lock``, and the operator
``cw dev-queue`` commands pass ``bounded=True`` so a wedged holder surfaces as
a clean, non-zero exit instead of a silent hang.
"""

from __future__ import annotations

import contextlib
import inspect
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

import cw.config
from cw import _flock
from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
from cw.cli import main
from cw.config import save_state
from cw.dev_queue import (
    add_ticket,
    approve_must_fix_override_ticket,
    approve_scope_drift_ticket,
    approve_ticket,
    cancel_task_for_session,
    cancel_ticket,
    clear_tickets,
    load_dev_queue,
    move_ticket,
    prune_tickets,
    register_watched_pr,
    remove_ticket,
    requeue_ticket,
    revoke_plan_approval,
    save_dev_queue,
)
from cw.dev_queue import storage as dev_queue_storage
from cw.dev_queue.crud import register_or_adopt_watched_pr
from cw.exceptions import CwError
from cw.models import CwState, DevQueueStore, QueueItemStatus
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import (
    _FakeClock,
    _hold_flock,
    _make_daemon_session,
    _make_ticket_task,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from cw.native_daemon import FakeNativeDaemonClient

_CLIENT = "acme"
# Dyadic: the fake clock lands on it exactly, so the message renders "0.5s"
# and "currently 0.5" with no rounding ambiguity.
_TIMEOUT_S = "0.5"


def _dev_queue_timeout_message(lock_path: Path) -> str:
    return (
        f"Timed out after 0.5s waiting for the dev_queue lock {lock_path}."
        " Another cw process is holding it, typically `cw dev-queue serve`."
        f" Find the holder with `lsof {lock_path}` (the lock file records no"
        " PID). If `cw dev-queue serve` has stopped emitting `dispatch.tick`"
        " events it is wedged: restart it. If the holder is merely slow, retry,"
        " or raise the wait with CW_SESSIONS_LOCK_TIMEOUT_S (seconds; currently"
        " 0.5)."
    )


@pytest.fixture
def acme(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A configured ``acme`` client (the lane-command test setup)."""
    write_clients_yaml(ClientSpec(_CLIENT, tmp_path / "ws"), ensure_workspaces=True)


@pytest.fixture
def fake_lock_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Bound every acquisition at 0.5s on a fake clock: no wall-clock waiting."""
    monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, _TIMEOUT_S)
    clock = _FakeClock()
    monkeypatch.setattr(_flock, "time", clock)
    return clock


# ---------------------------------------------------------------------------
# Library forwarding
# ---------------------------------------------------------------------------

_LOCK_MODULE = {
    "crud": "cw.dev_queue.crud._lock",
    "approval": "cw.dev_queue.approval._lock",
    "must_fix_override": "cw.dev_queue.must_fix_override._lock",
    "requeue": "cw.dev_queue.requeue._lock",
}


def _forwarding_cases(
    daemon: FakeNativeDaemonClient,
) -> dict[str, tuple[str, Callable[..., object]]]:
    """``name -> (module key, call taking **kwargs)`` for every bounded function."""
    task = _make_ticket_task(ticket_id="T-1", client=_CLIENT)
    return {
        "add_ticket": ("crud", lambda **kw: add_ticket(task, **kw)),
        "move_ticket": (
            "crud",
            lambda **kw: move_ticket("T-1", _CLIENT, priority=1, **kw),
        ),
        "remove_ticket": ("crud", lambda **kw: remove_ticket("T-1", _CLIENT, **kw)),
        "cancel_ticket": ("crud", lambda **kw: cancel_ticket("T-1", _CLIENT, **kw)),
        "clear_tickets": ("crud", lambda **kw: clear_tickets(_CLIENT, **kw)),
        "prune_tickets": (
            "crud",
            lambda **kw: prune_tickets(
                frozenset({QueueItemStatus.COMPLETED}), client=_CLIENT, **kw
            ),
        ),
        "approve_ticket": (
            "approval",
            lambda **kw: approve_ticket("T-1", _CLIENT, **kw),
        ),
        "revoke_plan_approval": (
            "approval",
            lambda **kw: revoke_plan_approval("T-1", _CLIENT, **kw),
        ),
        "approve_scope_drift_ticket": (
            "approval",
            lambda **kw: approve_scope_drift_ticket("T-1", _CLIENT, ["a.py"], **kw),
        ),
        "approve_must_fix_override_ticket": (
            "must_fix_override",
            lambda **kw: approve_must_fix_override_ticket(
                "T-1", _CLIENT, "operator reason", **kw
            ),
        ),
        "requeue_ticket": (
            "requeue",
            lambda **kw: requeue_ticket("T-1", _CLIENT, native_daemon=daemon, **kw),
        ),
    }


_FORWARDING_NAMES = [
    "add_ticket",
    "move_ticket",
    "remove_ticket",
    "cancel_ticket",
    "clear_tickets",
    "prune_tickets",
    "approve_ticket",
    "revoke_plan_approval",
    "approve_scope_drift_ticket",
    "approve_must_fix_override_ticket",
    "requeue_ticket",
]


class TestLibraryForwardsBounded:
    @pytest.mark.parametrize("name", _FORWARDING_NAMES)
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [({"bounded": True}, True), ({}, False)],
        ids=["bounded", "omitted"],
    )
    def test_forwards_bounded_to_the_dev_queue_lock(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_native_daemon: FakeNativeDaemonClient,
        name: str,
        kwargs: dict[str, bool],
        expected: bool,
    ) -> None:
        recorded: list[bool] = []

        @contextlib.contextmanager
        def _recording_lock(*, bounded: bool = False) -> Iterator[None]:
            recorded.append(bounded)
            yield

        module_key, call = _forwarding_cases(mock_native_daemon)[name]
        monkeypatch.setattr(_LOCK_MODULE[module_key], _recording_lock)

        # The empty queue makes most calls raise "not found" AFTER lock entry.
        with contextlib.suppress(CwError):
            call(**kwargs)

        assert recorded == [expected]

    @pytest.mark.parametrize(
        "fn",
        [register_watched_pr, register_or_adopt_watched_pr, cancel_task_for_session],
        ids=lambda fn: fn.__name__,
    )
    def test_shared_and_post_side_effect_writers_take_no_bounded(
        self, fn: Callable[..., object]
    ) -> None:
        """Unattended or commit-after-side-effect callers must keep waiting."""
        assert "bounded" not in inspect.signature(fn).parameters


# ---------------------------------------------------------------------------
# Single-command CLI contention
# ---------------------------------------------------------------------------

_SINGLE_COMMANDS: dict[str, list[str]] = {
    "add": ["dev-queue", "add", "T-9", "-c", _CLIENT],
    "move": ["dev-queue", "move", "T-1", "-c", _CLIENT, "-p", "5"],
    "requeue": ["dev-queue", "requeue", "T-1", "-c", _CLIENT],
    "remove": ["dev-queue", "remove", "T-1", "-c", _CLIENT],
    "cancel": ["dev-queue", "cancel", "T-1", "-c", _CLIENT],
    "clear": ["dev-queue", "clear", "-c", _CLIENT, "--confirm"],
    "prune": ["dev-queue", "prune", "-c", _CLIENT, "--confirm"],
    "approve": ["dev-queue", "approve", "T-1", "-c", _CLIENT],
    "approve-scope-drift": [
        "dev-queue",
        "approve",
        "T-1",
        "-c",
        _CLIENT,
        "--scope-drift",
        "a.py",
    ],
    "approve-override-must-fix": [
        "dev-queue",
        "approve",
        "T-1",
        "-c",
        _CLIENT,
        "--override-must-fix",
        "--reason",
        "operator reason",
    ],
    "revoke-plan-approval": ["dev-queue", "revoke-plan-approval", "T-1", "-c", _CLIENT],
}


class TestSingleCommandContention:
    @pytest.mark.parametrize("command", list(_SINGLE_COMMANDS))
    @pytest.mark.usefixtures("acme", "fake_lock_clock")
    def test_times_out_cleanly_and_leaves_the_queue_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_native_daemon: FakeNativeDaemonClient,
        command: str,
    ) -> None:
        monkeypatch.setattr(
            "cw.dev_queue.requeue.get_native_daemon_client", lambda: mock_native_daemon
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.get_native_daemon_client", lambda: mock_native_daemon
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="T-1",
                        client=_CLIENT,
                        status=QueueItemStatus.BLOCKED_ON_USER,
                    )
                ]
            )
        )
        queue_before = cw.config.dev_queue_file().read_bytes()
        lock_path = cw.config.dev_queue_lock()

        with _hold_flock(lock_path):
            result = CliRunner().invoke(main, _SINGLE_COMMANDS[command])

        assert result.exit_code != 0
        assert _dev_queue_timeout_message(lock_path) in result.output
        assert cw.config.dev_queue_file().read_bytes() == queue_before


# ---------------------------------------------------------------------------
# Multi-ticket CLI contention: stop at the first timeout, report done/remaining
# ---------------------------------------------------------------------------


_CONTENDED_CALL = 2


def _contend_second_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the 2nd ``crud._lock`` acquisition meet a held dev-queue lock.

    The real bounded ``_lock`` runs every time; only the 2nd call happens
    while another open file description holds the lock. Deterministic, no
    threads.
    """
    real_lock = dev_queue_storage._lock
    calls = 0

    @contextlib.contextmanager
    def _wrapped(*, bounded: bool = False) -> Iterator[None]:
        nonlocal calls
        calls += 1
        if calls == _CONTENDED_CALL:
            with _hold_flock(cw.config.dev_queue_lock()), real_lock(bounded=bounded):
                yield
        else:
            with real_lock(bounded=bounded):
                yield

    monkeypatch.setattr("cw.dev_queue.crud._lock", _wrapped)


def _queue_by_ticket() -> dict[str, QueueItemStatus]:
    return {t.ticket_id: t.status for t in load_dev_queue().tasks}


@pytest.mark.usefixtures("acme", "fake_lock_clock")
class TestMultiTicketContention:
    def test_add_stops_at_first_timeout_and_names_done_and_remaining(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _contend_second_acquisition(monkeypatch)

        result = CliRunner().invoke(main, ["dev-queue", "add", "A", "B", "-c", _CLIENT])

        assert result.exit_code != 0
        assert _queue_by_ticket() == {"A": QueueItemStatus.PENDING}
        assert (
            "Enqueued or already queued: A. Not enqueued (lock timed out): B."
            " `cw dev-queue add` skips tickets that are already queued, so"
            " re-running the full command is safe."
        ) in result.stderr
        assert _dev_queue_timeout_message(cw.config.dev_queue_lock()) in result.stderr

    def test_remove_stops_at_first_timeout_with_the_not_idempotent_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(ticket_id="A", client=_CLIENT),
                    _make_ticket_task(ticket_id="B", client=_CLIENT),
                ]
            )
        )
        _contend_second_acquisition(monkeypatch)

        result = CliRunner().invoke(
            main, ["dev-queue", "remove", "A", "B", "-c", _CLIENT]
        )

        assert result.exit_code != 0
        assert _queue_by_ticket() == {"B": QueueItemStatus.PENDING}
        assert (
            "Removed: A. Not removed (lock timed out): B. `remove` is not"
            " idempotent: re-run it for the not-removed tickets only, with the"
            " same options; repeating a removed ticket fails with 'No dev-queue"
            " task found'."
        ) in result.stderr
        assert _dev_queue_timeout_message(cw.config.dev_queue_lock()) in result.stderr

    def test_cancel_stops_at_first_timeout_and_stops_only_done_sessions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_native_daemon: FakeNativeDaemonClient,
    ) -> None:
        save_state(
            CwState(
                sessions=[
                    _make_daemon_session(
                        id="sess-a", name=f"{_CLIENT}/auto-dev/A", surface_ref="ref-a"
                    ),
                    _make_daemon_session(
                        id="sess-b", name=f"{_CLIENT}/auto-dev/B", surface_ref="ref-b"
                    ),
                ]
            )
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="A",
                        client=_CLIENT,
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-a",
                    ),
                    _make_ticket_task(
                        ticket_id="B",
                        client=_CLIENT,
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-b",
                    ),
                ]
            )
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.crud.get_native_daemon_client", lambda: mock_native_daemon
        )
        _contend_second_acquisition(monkeypatch)

        result = CliRunner().invoke(
            main, ["dev-queue", "cancel", "A", "B", "-c", _CLIENT]
        )

        assert result.exit_code != 0
        assert _queue_by_ticket() == {
            "A": QueueItemStatus.CANCELLED,
            "B": QueueItemStatus.RUNNING,
        }
        assert mock_native_daemon.stop_calls == ["ref-a"]
        assert (
            "Cancelled: A. Not cancelled (lock timed out): B. `cancel` is"
            " idempotent: re-running the full command is safe."
        ) in result.stderr
        assert _dev_queue_timeout_message(cw.config.dev_queue_lock()) in result.stderr

    def test_timeout_at_the_first_ticket_reports_none_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(ticket_id="A", client=_CLIENT),
                    _make_ticket_task(ticket_id="B", client=_CLIENT),
                ]
            )
        )

        with _hold_flock(cw.config.dev_queue_lock()):
            result = CliRunner().invoke(
                main, ["dev-queue", "remove", "A", "B", "-c", _CLIENT]
            )

        assert result.exit_code != 0
        assert set(_queue_by_ticket()) == {"A", "B"}
        assert "Removed: (none). Not removed (lock timed out): A, B." in result.stderr

    def test_single_ticket_timeout_prints_no_partial_summary(self) -> None:
        save_dev_queue(
            DevQueueStore(tasks=[_make_ticket_task(ticket_id="A", client=_CLIENT)])
        )

        with _hold_flock(cw.config.dev_queue_lock()):
            result = CliRunner().invoke(
                main, ["dev-queue", "remove", "A", "-c", _CLIENT]
            )

        assert result.exit_code != 0
        assert "Not removed (lock timed out)" not in result.stderr
        assert "dev_queue lock" in result.stderr
