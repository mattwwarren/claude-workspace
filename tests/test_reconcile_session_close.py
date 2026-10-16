"""Tests for ``cw.reconcile.session_close`` (#2694).

The module owns three things: the in-lock close body shared by ``cw spawn
close`` and reconcile (``close_session_in_state``, with the flip-only
``settle_row=False`` variant the cancel path uses), the lock-free queued-close
file store, and the two appliers (``apply_queued_closes`` from reconcile,
``close_sessions_for_cancel`` from ``cw dev-queue cancel``).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from cw.reconcile.session_close import (
    CancelCloseOutcome,
    apply_queued_closes,
    clear_queued_close,
    close_session_in_state,
    close_sessions_for_cancel,
    pending_queued_closes,
    queue_close,
    queued_closes_dir,
)

from cw import _flock
from cw.config import (
    clients_file,
    dev_queue_file,
    load_state,
    save_state,
    sessions_lock,
    state_dir,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import (
    CompletionReason,
    CwState,
    DevQueueStore,
    LastResultSource,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import session_close
from cw.reconcile.deferred import DeferredReconcileJobs, is_surface_stop_queued
from tests.conftest import _FakeClock, _make_daemon_session, _make_ticket_task
from tests.test_result import _valid_payload

if TYPE_CHECKING:
    from pathlib import Path

_LOGGER = "cw.reconcile.session_close"
_CLIENT = "test-client"
_TICKET = "GEN-1234"
_NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
_DEV_QUEUE_SYMBOLS = (
    "cancel_task_for_session",
    "running_task_for_session",
    "load_dev_queue",
    "route_staged_emit_result",
)


def _session(sid: str = "sess-1", **overrides: object) -> Session:
    fields: dict[str, object] = {
        "id": sid,
        "name": f"{_CLIENT}/my-task-{sid}",
        "client": _CLIENT,
        "surface_ref": f"ref-{sid}",
    }
    fields.update(overrides)
    return _make_daemon_session(**fields)


def _seed(*sessions: Session) -> None:
    save_state(CwState(sessions=list(sessions)))


def _row(
    sid: str | None = "sess-1",
    *,
    status: QueueItemStatus = QueueItemStatus.RUNNING,
    stage: Stage = Stage.FINALIZE,
) -> TicketTask:
    return _make_ticket_task(
        ticket_id=_TICKET,
        client=_CLIENT,
        status=status,
        session_id=sid,
        stage=stage,
        attempts=1,
    )


def _seed_row(row: TicketTask) -> None:
    save_dev_queue(DevQueueStore(tasks=[row]))


def _only_row() -> TicketTask:
    (row,) = load_dev_queue().tasks
    return row


def _stored(sid: str) -> Session:
    sess = next((s for s in load_state().sessions if s.id == sid), None)
    assert sess is not None
    return sess


def _stage_emit_cli_result(sid: str) -> None:
    """Give *sid* a staged ``cw result emit`` shipped result (the #2458 shape)."""
    state = load_state()
    stored = state.find_by_name_or_id(sid)
    assert stored is not None
    stored.last_result = _valid_payload()
    stored.last_result_source = LastResultSource.EMIT_CLI
    save_state(state)
    path = clients_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "clients:\n"
        f"  {_CLIENT}:\n"
        "    workspace_path: /tmp/ws-close-2694\n"
        "    default_branch: main\n"
        "    pipeline:\n"
        "      stages: [plan, impl, review, finalize]\n"
    )


def _forbid_dev_queue_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every row-settling symbol fail loudly if the flip-only path calls it."""
    for name in _DEV_QUEUE_SYMBOLS:

        def _forbidden(*_args: object, _name: str = name, **_kwargs: object) -> None:
            msg = f"flip-only close called {_name}"
            raise AssertionError(msg)

        monkeypatch.setattr(f"cw.reconcile.session_close.{name}", _forbidden)


def _close_in_lock(sid: str, *, settle_row: bool = True) -> str | None:
    with sessions_lock():
        state = load_state()
        sess = state.find_by_name_or_id(sid)
        assert sess is not None
        surface = close_session_in_state(state, sess, settle_row=settle_row)
        save_state(state)
    return surface


def _apply(deferred: DeferredReconcileJobs | None = None) -> int:
    jobs = deferred if deferred is not None else DeferredReconcileJobs()
    with sessions_lock():
        state = load_state()
        return apply_queued_closes(state, deferred=jobs)


def _messages(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(f"{event}:")]


def _entry_path(sid: str) -> Path:
    return queued_closes_dir() / f"{sid}.json"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_queue_close_writes_the_fenced_entry(self, tmp_config_dir: Path) -> None:
        resumed = _NOW - timedelta(hours=1)
        sess = _session(resumed_at=resumed)

        path = queue_close(sess, origin="spawn_close", now=_NOW)

        assert path == state_dir() / "queued_closes" / "sess-1.json"
        assert json.loads(path.read_text()) == {
            "session_id": "sess-1",
            "queued_at": _NOW.isoformat(),
            "origin": "spawn_close",
            "resumed_at": resumed.isoformat(),
            "surface_ref": "ref-sess-1",
        }

    def test_second_queue_for_the_same_id_overwrites(
        self, tmp_config_dir: Path
    ) -> None:
        sess = _session()

        queue_close(sess, origin="spawn_close", now=_NOW)
        queue_close(sess, origin="cancel", now=_NOW + timedelta(seconds=5))

        assert [p.name for p in queued_closes_dir().iterdir()] == ["sess-1.json"]
        (entry,) = pending_queued_closes()
        assert entry.origin == "cancel"
        assert entry.queued_at == _NOW + timedelta(seconds=5)

    def test_tmp_leftovers_are_ignored(self, tmp_config_dir: Path) -> None:
        queue_close(_session(), origin="cancel", now=_NOW)
        (queued_closes_dir() / "sess-2.json.abc123.tmp").write_text("{}")

        assert [e.session_id for e in pending_queued_closes()] == ["sess-1"]
        assert (queued_closes_dir() / "sess-2.json.abc123.tmp").exists()

    @pytest.mark.parametrize("bad_id", ["../x", "", ".hidden", "a/b"])
    def test_unsafe_ids_raise_and_write_nothing(
        self, tmp_config_dir: Path, bad_id: str
    ) -> None:
        sess = _session().model_copy(update={"id": bad_id})

        with pytest.raises(ValueError, match="unsafe session id"):
            queue_close(sess, origin="cancel", now=_NOW)

        assert not queued_closes_dir().exists() or not any(
            queued_closes_dir().iterdir()
        )

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "{not json",
            "[]",
            json.dumps({"session_id": "sess-9"}),
            json.dumps(
                {
                    "session_id": "sess-9",
                    "queued_at": _NOW.isoformat(),
                    "origin": "reap",
                    "resumed_at": None,
                    "surface_ref": None,
                }
            ),
            json.dumps(
                {
                    "session_id": "sess-9",
                    "queued_at": "not-a-time",
                    "origin": "cancel",
                    "resumed_at": None,
                    "surface_ref": None,
                }
            ),
            json.dumps(
                {
                    "session_id": "sess-9",
                    "queued_at": _NOW.isoformat(),
                    "origin": "cancel",
                    "resumed_at": None,
                    "surface_ref": 7,
                }
            ),
        ],
        ids=[
            "zero-length",
            "malformed",
            "not-an-object",
            "missing-keys",
            "unknown-origin",
            "bad-queued-at",
            "surface-wrong-type",
        ],
    )
    def test_corrupt_entries_are_removed_with_a_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture, content: str
    ) -> None:
        queued_closes_dir().mkdir(parents=True)
        corrupt = queued_closes_dir() / "sess-9.json"
        corrupt.write_text(content)
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        assert pending_queued_closes() == []

        assert not corrupt.exists()
        (record,) = _messages(caplog, "queued_close_corrupt")
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert f"path={corrupt}" in message
        assert "error=" in message
        assert "action=entry_removed" in message

    def test_pending_lists_oldest_first(self, tmp_config_dir: Path) -> None:
        queue_close(_session("b-newer"), origin="cancel", now=_NOW)
        queue_close(
            _session("a-older"), origin="cancel", now=_NOW - timedelta(minutes=1)
        )

        assert [e.session_id for e in pending_queued_closes()] == [
            "a-older",
            "b-newer",
        ]

    def test_missing_dir_lists_nothing(self, tmp_config_dir: Path) -> None:
        assert pending_queued_closes() == []

    def test_clear_of_a_missing_entry_is_a_no_op(self, tmp_config_dir: Path) -> None:
        clear_queued_close("never-queued")

        queue_close(_session(), origin="cancel", now=_NOW)
        clear_queued_close("sess-1")
        clear_queued_close("sess-1")

        assert pending_queued_closes() == []


# ---------------------------------------------------------------------------
# close_session_in_state, default (spawn close) semantics
# ---------------------------------------------------------------------------


class TestCloseSessionInStateFull:
    def test_cancels_the_running_row_and_stamps_completed_user(
        self, tmp_config_dir: Path
    ) -> None:
        _seed(_session())
        _seed_row(_row())

        surface = _close_in_lock("sess-1")

        assert surface == "ref-sess-1"
        assert _only_row().status == QueueItemStatus.CANCELLED
        closed = _stored("sess-1")
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        assert closed.completed_at is not None

    def test_staged_emit_cli_result_routes_instead_of_cancelling(
        self, tmp_config_dir: Path
    ) -> None:
        _seed(_session())
        _stage_emit_cli_result("sess-1")
        _seed_row(_row())

        _close_in_lock("sess-1")

        assert _only_row().status == QueueItemStatus.COMPLETED
        assert _stored("sess-1").status == SessionStatus.COMPLETED

    def test_already_completed_returns_surface_and_mutates_nothing(
        self, tmp_config_dir: Path
    ) -> None:
        done_at = _NOW - timedelta(days=1)
        _seed(
            _session(
                status=SessionStatus.COMPLETED,
                completed_at=done_at,
                completed_reason=CompletionReason.CRASHED,
            )
        )
        _seed_row(_row())

        surface = _close_in_lock("sess-1")

        assert surface == "ref-sess-1"
        assert _only_row().status == QueueItemStatus.RUNNING
        closed = _stored("sess-1")
        assert closed.completed_reason == CompletionReason.CRASHED
        assert closed.completed_at == done_at

    def test_timed_out_is_still_stamped_completed_user(
        self, tmp_config_dir: Path
    ) -> None:
        _seed(_session(status=SessionStatus.TIMED_OUT))

        _close_in_lock("sess-1")

        closed = _stored("sess-1")
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER

    def test_user_origin_legacy_surface_warns_and_returns_none(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _seed(_session(origin=SessionOrigin.USER, surface_ref="tmux-pane-7"))
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        surface = _close_in_lock("sess-1")

        assert surface is None
        assert "legacy surface_ref" in caplog.text
        assert _stored("sess-1").status == SessionStatus.COMPLETED

    def test_no_surface_ref_returns_none(self, tmp_config_dir: Path) -> None:
        _seed(_session(surface_ref=None))

        assert _close_in_lock("sess-1") is None
        assert _stored("sess-1").status == SessionStatus.COMPLETED


# ---------------------------------------------------------------------------
# close_session_in_state(settle_row=False): flip-only (the cancel semantics)
# ---------------------------------------------------------------------------


class TestCloseSessionInStateFlipOnly:
    def test_flips_without_touching_the_queue(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())
        _seed_row(_row())
        _forbid_dev_queue_steps(monkeypatch)

        surface = _close_in_lock("sess-1", settle_row=False)

        assert surface == "ref-sess-1"
        closed = _stored("sess-1")
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        assert closed.completed_at is not None
        assert _only_row().status == QueueItemStatus.RUNNING

    def test_staged_result_is_not_routed(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())
        _stage_emit_cli_result("sess-1")
        _seed_row(_row())
        _forbid_dev_queue_steps(monkeypatch)

        _close_in_lock("sess-1", settle_row=False)

        assert _only_row().status == QueueItemStatus.RUNNING

    @pytest.mark.parametrize(
        "status", [SessionStatus.TIMED_OUT, SessionStatus.COMPLETED]
    )
    def test_terminal_session_returns_surface_and_mutates_nothing(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        status: SessionStatus,
    ) -> None:
        _seed(_session(status=status))
        _forbid_dev_queue_steps(monkeypatch)

        surface = _close_in_lock("sess-1", settle_row=False)

        assert surface == "ref-sess-1"
        stored = _stored("sess-1")
        assert stored.status == status
        assert stored.completed_reason is None

    def test_user_origin_legacy_surface_warns_and_still_flips(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seed(_session(origin=SessionOrigin.USER, surface_ref="tmux-pane-7"))
        _forbid_dev_queue_steps(monkeypatch)
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        assert _close_in_lock("sess-1", settle_row=False) is None
        assert "legacy surface_ref" in caplog.text
        assert _stored("sess-1").status == SessionStatus.COMPLETED

    def test_no_surface_ref_returns_none(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session(surface_ref=None))
        _forbid_dev_queue_steps(monkeypatch)

        assert _close_in_lock("sess-1", settle_row=False) is None


# ---------------------------------------------------------------------------
# apply_queued_closes
# ---------------------------------------------------------------------------


class TestApplyQueuedCloses:
    def test_applies_saves_clears_and_queues_one_stop(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        sess = _session()
        _seed(sess)
        queue_close(sess, origin="spawn_close", now=_NOW)
        jobs = DeferredReconcileJobs()
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        assert _apply(jobs) == 1

        assert _stored("sess-1").status == SessionStatus.COMPLETED
        assert pending_queued_closes() == []
        assert is_surface_stop_queued(jobs, "ref-sess-1")
        assert len(jobs.post_lock) == 1
        (record,) = _messages(caplog, "queued_close_applied")
        assert record.levelno == logging.WARNING
        assert record.getMessage() == (
            "queued_close_applied: session=sess-1 origin=spawn_close"
            f" settle_row=True queued_at={_NOW.isoformat()} prior_status=active"
            " surface=ref-sess-1"
        )

    def test_spawn_close_entry_routes_a_staged_result(
        self, tmp_config_dir: Path
    ) -> None:
        sess = _session()
        _seed(sess)
        _stage_emit_cli_result("sess-1")
        _seed_row(_row())
        queue_close(_stored("sess-1"), origin="spawn_close", now=_NOW)

        _apply()

        assert _only_row().status == QueueItemStatus.COMPLETED
        assert _stored("sess-1").status == SessionStatus.COMPLETED

    def test_spawn_close_entry_cancels_the_row_without_a_staged_result(
        self, tmp_config_dir: Path
    ) -> None:
        sess = _session()
        _seed(sess)
        _seed_row(_row())
        queue_close(sess, origin="spawn_close", now=_NOW)

        _apply()

        assert _only_row().status == QueueItemStatus.CANCELLED

    def test_cancel_entry_is_flip_only(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sess = _session()
        _seed(sess)
        _seed_row(_row(None, status=QueueItemStatus.CANCELLED))
        queue_before = dev_queue_file().read_bytes()
        queue_close(sess, origin="cancel", now=_NOW)
        _forbid_dev_queue_steps(monkeypatch)
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        assert _apply() == 1

        closed = _stored("sess-1")
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        assert dev_queue_file().read_bytes() == queue_before
        (record,) = _messages(caplog, "queued_close_applied")
        assert "origin=cancel settle_row=False" in record.getMessage()

    def test_cancel_entry_for_a_timed_out_session_clears_and_still_stops(
        self, tmp_config_dir: Path
    ) -> None:
        sess = _session()
        _seed(sess)
        queue_close(sess, origin="cancel", now=_NOW)
        state = load_state()
        state.sessions[0].status = SessionStatus.TIMED_OUT
        save_state(state)
        jobs = DeferredReconcileJobs()

        _apply(jobs)

        assert _stored("sess-1").status == SessionStatus.TIMED_OUT
        assert pending_queued_closes() == []
        assert is_surface_stop_queued(jobs, "ref-sess-1")

    def test_unknown_session_clears_the_entry_with_a_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        queue_close(_session("gone-1"), origin="cancel", now=_NOW)
        _seed()
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        assert _apply() == 0

        assert pending_queued_closes() == []
        (record,) = _messages(caplog, "queued_close_unknown_session")
        assert record.levelno == logging.WARNING
        assert record.getMessage() == (
            "queued_close_unknown_session: session=gone-1 origin=cancel"
            f" queued_at={_NOW.isoformat()} action=entry_removed"
        )

    @pytest.mark.parametrize(
        ("field", "value", "mismatched"),
        [
            ("resumed_at", _NOW, "resumed_at"),
            ("surface_ref", "ref-new", "surface_ref"),
        ],
    )
    def test_fence_mismatch_voids_the_entry(
        self,
        tmp_config_dir: Path,
        caplog: pytest.LogCaptureFixture,
        field: str,
        value: object,
        mismatched: str,
    ) -> None:
        sess = _session()
        _seed(sess)
        queue_close(sess, origin="spawn_close", now=_NOW)
        resumed = sess.model_copy(update={field: value})
        _seed(resumed)
        jobs = DeferredReconcileJobs()
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        assert _apply(jobs) == 0

        assert _stored("sess-1").status == SessionStatus.ACTIVE
        assert pending_queued_closes() == []
        assert jobs.post_lock == []
        (record,) = _messages(caplog, "queued_close_void")
        message = record.getMessage()
        assert message.startswith(
            f"queued_close_void: session=sess-1 origin=spawn_close"
            f" queued_at={_NOW.isoformat()} mismatched={mismatched} "
        )
        assert "entry_resumed_at=None" in message
        assert "entry_surface_ref=ref-sess-1" in message
        current_surface = value if field == "surface_ref" else "ref-sess-1"
        assert f"current_surface_ref={current_surface}" in message

    def test_both_fence_fields_mismatched_are_named(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        sess = _session()
        _seed(sess)
        queue_close(sess, origin="cancel", now=_NOW)
        _seed(sess.model_copy(update={"resumed_at": _NOW, "surface_ref": "ref-new"}))
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        _apply()

        (record,) = _messages(caplog, "queued_close_void")
        assert "mismatched=resumed_at,surface_ref" in record.getMessage()

    def test_none_equals_none_fence_applies(self, tmp_config_dir: Path) -> None:
        sess = _session(resumed_at=None)
        _seed(sess)
        queue_close(sess, origin="cancel", now=_NOW)

        assert _apply() == 1

    def test_second_apply_is_a_no_op(self, tmp_config_dir: Path) -> None:
        sess = _session()
        _seed(sess)
        queue_close(sess, origin="spawn_close", now=_NOW)

        assert _apply() == 1
        assert _apply() == 0

    def test_already_completed_session_clears_and_still_stops(
        self, tmp_config_dir: Path
    ) -> None:
        sess = _session()
        _seed(sess)
        queue_close(sess, origin="spawn_close", now=_NOW)
        _seed(sess.model_copy(update={"status": SessionStatus.COMPLETED}))
        jobs = DeferredReconcileJobs()

        _apply(jobs)

        assert pending_queued_closes() == []
        assert is_surface_stop_queued(jobs, "ref-sess-1")

    def test_a_failing_entry_is_kept_and_the_next_still_applies(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        first = _session("sess-a")
        second = _session("sess-b")
        _seed(first, second)
        queue_close(first, origin="spawn_close", now=_NOW - timedelta(minutes=1))
        queue_close(second, origin="spawn_close", now=_NOW)
        real_close = session_close.close_session_in_state

        def _poison(state: CwState, sess: Session, **kwargs: bool) -> str | None:
            if sess.id == "sess-a":
                msg = "poison entry"
                raise CwError(msg)
            return real_close(state, sess, **kwargs)

        monkeypatch.setattr(
            "cw.reconcile.session_close.close_session_in_state", _poison
        )
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        assert _apply() == 1

        assert [e.session_id for e in pending_queued_closes()] == ["sess-a"]
        assert _stored("sess-a").status == SessionStatus.ACTIVE
        assert _stored("sess-b").status == SessionStatus.COMPLETED
        (record,) = _messages(caplog, "queued_close_failed")
        assert record.levelno == logging.WARNING
        assert record.exc_info is not None
        assert record.getMessage() == (
            "queued_close_failed: session=sess-a origin=spawn_close"
            f" queued_at={(_NOW - timedelta(minutes=1)).isoformat()}"
            " action=entry_kept"
        )

    def test_emits_no_orchestrator_event(self, tmp_config_dir: Path) -> None:
        sess = _session()
        _seed(sess)
        queue_close(sess, origin="spawn_close", now=_NOW)

        _apply()

        assert read_events(consumer="test-2694-fresh-consumer") == []

    def test_nothing_queued_returns_zero_without_saving(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())

        def _no_save(_state: CwState) -> None:
            msg = "nothing to apply must not save"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.reconcile.session_close.save_state", _no_save)

        assert _apply() == 0


# ---------------------------------------------------------------------------
# close_sessions_for_cancel
# ---------------------------------------------------------------------------


class TestCloseSessionsForCancel:
    def test_flips_a_live_daemon_session_flip_only(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())
        _forbid_dev_queue_steps(monkeypatch)

        outcome = close_sessions_for_cancel(["sess-1"], timeout_s=5.0, skip_lock=False)

        assert outcome == CancelCloseOutcome(
            surfaces_to_stop=("ref-sess-1",), pending_ids=(), unrecorded_ids=()
        )
        closed = _stored("sess-1")
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        assert pending_queued_closes() == []

    def test_user_origin_session_is_untouched(self, tmp_config_dir: Path) -> None:
        _seed(_session(origin=SessionOrigin.USER))

        outcome = close_sessions_for_cancel(["sess-1"], timeout_s=5.0, skip_lock=False)

        assert outcome == CancelCloseOutcome((), (), ())
        assert _stored("sess-1").status == SessionStatus.ACTIVE
        assert pending_queued_closes() == []

    @pytest.mark.parametrize(
        "status", [SessionStatus.COMPLETED, SessionStatus.TIMED_OUT]
    )
    def test_terminal_session_queues_nothing_but_is_still_stopped(
        self, tmp_config_dir: Path, status: SessionStatus
    ) -> None:
        _seed(_session(status=status))

        outcome = close_sessions_for_cancel(["sess-1"], timeout_s=5.0, skip_lock=False)

        assert outcome.surfaces_to_stop == ("ref-sess-1",)
        assert outcome.pending_ids == ()
        assert _stored("sess-1").status == status
        assert pending_queued_closes() == []

    def test_none_and_unknown_ids_are_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())
        clock = _FakeClock()
        monkeypatch.setattr(_flock, "time", clock)

        outcome = close_sessions_for_cancel(
            [None, "nobody"], timeout_s=5.0, skip_lock=False
        )

        assert outcome == CancelCloseOutcome((), (), ())
        assert _stored("sess-1").status == SessionStatus.ACTIVE

    def test_held_lock_queues_and_keeps_the_entry(
        self, held_sessions_lock: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())
        clock = _FakeClock()
        monkeypatch.setattr(_flock, "time", clock)

        outcome = close_sessions_for_cancel(["sess-1"], timeout_s=5.0, skip_lock=False)

        assert outcome == CancelCloseOutcome(
            surfaces_to_stop=("ref-sess-1",),
            pending_ids=("sess-1",),
            unrecorded_ids=(),
        )
        assert clock.now == 5.0
        assert _stored("sess-1").status == SessionStatus.ACTIVE
        (entry,) = pending_queued_closes()
        assert entry.origin == "cancel"
        assert entry.surface_ref == "ref-sess-1"

    def test_skip_lock_makes_no_attempt_and_keeps_the_entry(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())
        clock = _FakeClock()
        monkeypatch.setattr(_flock, "time", clock)

        outcome = close_sessions_for_cancel(["sess-1"], timeout_s=5.0, skip_lock=True)

        assert outcome.pending_ids == ("sess-1",)
        assert outcome.surfaces_to_stop == ("ref-sess-1",)
        assert clock.sleeps == []
        assert _stored("sess-1").status == SessionStatus.ACTIVE
        assert [e.session_id for e in pending_queued_closes()] == ["sess-1"]

    def test_fence_mismatch_under_the_lock_voids_and_does_not_flip(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = _session()
        _seed(stale.model_copy(update={"resumed_at": _NOW, "surface_ref": "ref-new"}))
        monkeypatch.setattr(
            "cw.reconcile.session_close.load_state",
            lambda: CwState(sessions=[stale]),
        )

        outcome = close_sessions_for_cancel(["sess-1"], timeout_s=5.0, skip_lock=False)

        assert outcome.pending_ids == ()
        assert _stored("sess-1").status == SessionStatus.ACTIVE
        assert pending_queued_closes() == []

    def test_entry_write_failure_is_reported_and_the_lock_still_tried(
        self, held_sessions_lock: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())
        clock = _FakeClock()
        monkeypatch.setattr(_flock, "time", clock)

        def _disk_full(_path: Path, _text: str) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("cw.reconcile.session_close.atomic_write_text", _disk_full)

        outcome = close_sessions_for_cancel(["sess-1"], timeout_s=5.0, skip_lock=False)

        assert outcome.unrecorded_ids == ("sess-1",)
        assert outcome.pending_ids == ()
        assert outcome.surfaces_to_stop == ("ref-sess-1",)
        assert clock.now == 5.0

    def test_entry_write_failure_still_flips_when_the_lock_is_free(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(_session())

        def _disk_full(_path: Path, _text: str) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("cw.reconcile.session_close.atomic_write_text", _disk_full)

        outcome = close_sessions_for_cancel(["sess-1"], timeout_s=5.0, skip_lock=False)

        assert outcome.unrecorded_ids == ()
        assert _stored("sess-1").status == SessionStatus.COMPLETED
