"""Tests for cw.reconcile.codex_reparks — reconcile-tick re-evaluation of
live-writer codex-orphan parks (GitHub #2307).

The boot pass (``cw.reconcile.codex_boot``) parks an orphan whose worktree may
still hold a codex writer and deliberately leaves its ``Session`` ACTIVE. The
row's own ``session_id`` is cleared on park, so ``codex_orphan_session_id`` is
the only link back to that session. These tests drive the sweep that follows
that link on every reconcile tick and, once the writer is affirmatively gone,
applies the boot pass's own clean-path logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import psutil
import pytest

from cw.config import (
    load_state,
    orchestrator_config_file,
    save_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    CompletionReason,
    CwState,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    SessionStatus,
    Stage,
)
from cw.reconcile import reconcile
from cw.reconcile.codex_boot import (
    _PARK_REASON_DIRTY_WORKTREE,
    _PARK_REASON_PROCESS_SCAN_INCONCLUSIVE,
    _PARK_REASON_REAP_POLICY_NOT_AUTO,
    CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
    CODEX_ORPHANED_AT_BOOT_DISPOSITION,
    _OrphanDisposition,
    reap_orphaned_codex_sessions_at_boot,
)
from cw.reconcile.codex_reparks import (
    _LIVE_WRITER_RESCAN_BACKOFF_SECONDS,
    CODEX_ORPHAN_CLEAN_REQUEUE_REASON_AT_RECONCILE,
    CODEX_ORPHAN_CLOSE_REASON_AT_RECONCILE,
    _act_on_live_writer_repark_candidates,
    _ReparkCandidate,
    run_codex_live_writer_reparks,
)
from tests.test_reconcile_codex_boot import (
    _completed_events,
    _FakeCodex,
    _forbid_os_kill,
    _live_writer,
    _no_codex_process,
    _requeued_events,
    _seed_clean_codex_orphan,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import Session, TicketTask

_PARKED_AT = datetime(2026, 1, 1, 0, 30, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
_TICKET = "T-orphan"
_CLIENT = "client-a"


def _config(*, auto: bool) -> OrchestratorConfig:
    return OrchestratorConfig(
        reap_policy=ReapPolicy.AUTO if auto else ReapPolicy.SIGNAL_ONLY
    )


def _run(*, auto: bool, now: datetime = _NOW) -> list[str]:
    """Run the sweep the way reconcile does: under the held sessions_lock."""
    with sessions_lock():
        return run_codex_live_writer_reparks(now=now, config=_config(auto=auto))


def _park_as_live_writer_orphan(
    *, rescan_at: datetime | None = None, linked: bool = True
) -> Session:
    """Put the seeded RUNNING row into the boot pass's live-writer park shape."""
    session = load_state().sessions[0]
    store = load_dev_queue()
    task = store.tasks[0]
    task.status = QueueItemStatus.BLOCKED_ON_USER
    task.disposition = CODEX_ORPHANED_AT_BOOT_DISPOSITION
    task.completed_at = _PARKED_AT
    task.session_id = None
    task.codex_orphan_session_id = session.id if linked else None
    task.codex_orphan_rescan_next_eligible_at = rescan_at
    save_dev_queue(store)
    return session


def _seed_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    *,
    rescan_at: datetime | None = None,
    linked: bool = True,
) -> tuple[Path, Session]:
    repo, _ = _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    return repo, _park_as_live_writer_orphan(rescan_at=rescan_at, linked=linked)


def _task() -> TicketTask:
    return load_dev_queue().tasks[0]


def _session() -> Session:
    return load_state().sessions[0]


def _spy_process_iter(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def _spy(attrs: object) -> list[object]:
        calls.append(attrs)
        return []

    monkeypatch.setattr(psutil, "process_iter", _spy)
    return calls


def _assert_still_parked(session: Session) -> TicketTask:
    """The park is intact and still linked to *session*."""
    task = _task()
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    assert task.codex_orphan_session_id == session.id
    assert task.session_id is None
    return task


def _assert_session_active() -> None:
    session = _session()
    assert session.status is SessionStatus.ACTIVE
    assert session.completed_at is None
    assert session.completed_reason is None


def _assert_session_closed() -> None:
    session = _session()
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_reason is CompletionReason.CRASHED
    assert session.completed_at is not None


def _assert_left_parked_and_unlinked() -> None:
    task = _task()
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    assert task.codex_orphan_session_id is None
    assert task.codex_orphan_rescan_next_eligible_at is None


def _close_audit(
    session: Session, *, disposition: str, detail: str
) -> dict[str, object]:
    return {
        "session_id": session.id,
        "session_name": session.name,
        "client": _CLIENT,
        "ticket_id": _TICKET,
        "crashed": True,
        "salvaged": False,
        "reason": CODEX_ORPHAN_CLOSE_REASON_AT_RECONCILE,
        "disposition": disposition,
        "detail": detail,
    }


def _requeue_payload(session: Session) -> dict[str, object]:
    return {
        "ticket_id": _TICKET,
        "client": _CLIENT,
        "from_stage": Stage.REVIEW,
        "to_stage": Stage.REVIEW,
        "reason": CODEX_ORPHAN_CLEAN_REQUEUE_REASON_AT_RECONCILE,
        "session_id": session.id,
    }


def test_writer_exits_between_ticks_closes_session_and_requeues(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer is gone and the tree is clean under ``auto``: the session
    closes with its audit event and the row goes back to PENDING."""
    _, session = _seed_parked(
        tmp_config_dir,
        tmp_path,
        make_git_repo,
        rescan_at=_NOW - timedelta(seconds=1),
    )
    _no_codex_process(monkeypatch)

    assert _run(auto=True) == [_TICKET]

    _assert_session_closed()
    assert _completed_events("test-reparks-exit-completed") == [
        _close_audit(
            session,
            disposition="requeued",
            detail=CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
        )
    ]
    task = _task()
    assert task.status is QueueItemStatus.PENDING
    assert task.stage is Stage.REVIEW
    assert task.session_id is None
    assert task.disposition is None
    assert task.codex_orphan_session_id is None
    assert task.codex_orphan_rescan_next_eligible_at is None
    assert _requeued_events("test-reparks-exit-requeued") == [_requeue_payload(session)]


def test_still_live_writer_leaves_everything_untouched(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A codex process still sits in the worktree: nothing closes, nothing is
    signalled, and only the rescan backoff moves. The return list is empty —
    a rescan that still finds a writer transitioned nothing to PENDING."""
    repo, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    writer = _FakeCodex(repo, pid=4242)
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [writer])
    sent = _forbid_os_kill(monkeypatch)

    assert _run(auto=True) == []

    assert writer.signals == []
    assert sent == []
    _assert_session_active()
    task = _assert_still_parked(session)
    assert task.codex_orphan_rescan_next_eligible_at == _NOW + timedelta(
        seconds=_LIVE_WRITER_RESCAN_BACKOFF_SECONDS
    )
    assert _completed_events("test-reparks-live-completed") == []
    assert _requeued_events("test-reparks-live-requeued") == []


def test_inconclusive_scan_stays_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A codex candidate whose cwd psutil could not read is never "no writer"."""
    repo, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    unreadable = _FakeCodex(repo, pid=4242)
    unreadable.info["cwd"] = None
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [unreadable])

    assert _run(auto=True) == []

    _assert_session_active()
    task = _assert_still_parked(session)
    assert task.codex_orphan_rescan_next_eligible_at == _NOW + timedelta(
        seconds=_LIVE_WRITER_RESCAN_BACKOFF_SECONDS
    )
    assert unreadable.signals == []
    assert _completed_events("test-reparks-inconclusive-completed") == []
    assert _requeued_events("test-reparks-inconclusive-requeued") == []


def test_row_still_in_backoff_window_is_not_rescanned(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside its backoff window the row costs no process-table scan at all;
    once the window elapses (``now`` equal to it) the scan runs again."""
    eligible_at = _NOW + timedelta(seconds=60)
    _, session = _seed_parked(
        tmp_config_dir, tmp_path, make_git_repo, rescan_at=eligible_at
    )
    calls = _spy_process_iter(monkeypatch)
    # A clean worktree with no writer would requeue on a real scan, so an
    # untouched row proves the scan was skipped, not merely inconclusive.

    assert _run(auto=True) == []

    assert calls == []
    _assert_session_active()
    task = _assert_still_parked(session)
    assert task.codex_orphan_rescan_next_eligible_at == eligible_at

    assert _run(auto=True, now=eligible_at) == [_TICKET]

    assert len(calls) == 1
    assert _task().status is QueueItemStatus.PENDING


def test_no_writer_but_reap_policy_not_auto_closes_session_leaves_park(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0006: the session closes (no writer is left to race), but without
    ``auto`` the row stays parked. The closed-but-not-requeued row is excluded
    from the return list."""
    _, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)

    assert _run(auto=False) == []

    _assert_session_closed()
    assert _completed_events("test-reparks-signal-only-completed") == [
        _close_audit(
            session,
            disposition="parked",
            detail=_PARK_REASON_REAP_POLICY_NOT_AUTO,
        )
    ]
    _assert_left_parked_and_unlinked()
    assert _requeued_events("test-reparks-signal-only-requeued") == []


def test_no_writer_but_dirty_worktree_closes_session_leaves_park(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    (repo / "extra.txt").write_text("stray\n")
    _no_codex_process(monkeypatch)

    assert _run(auto=True) == []

    _assert_session_closed()
    assert _completed_events("test-reparks-dirty-completed") == [
        _close_audit(session, disposition="parked", detail=_PARK_REASON_DIRTY_WORKTREE)
    ]
    _assert_left_parked_and_unlinked()
    assert _requeued_events("test-reparks-dirty-requeued") == []


@pytest.mark.parametrize("auto", [True, False])
def test_session_already_closed_reruns_clean_gate_and_requeues(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto: bool,
) -> None:
    """Crash recovery: a prior tick closed the session but died before the row
    write. The session is not closed twice, but the clean-requeue gate still
    decides the row, and the linkage is cleared either way."""
    _, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    state = load_state()
    state.sessions[0].status = SessionStatus.COMPLETED
    state.sessions[0].completed_reason = CompletionReason.CRASHED
    state.sessions[0].completed_at = _NOW
    save_state(state)
    _no_codex_process(monkeypatch)

    assert _run(auto=auto) == ([_TICKET] if auto else [])

    assert _completed_events(f"test-reparks-crash-recovery-{auto}") == []
    assert _session().completed_at == _NOW
    if auto:
        task = _task()
        assert task.status is QueueItemStatus.PENDING
        assert task.codex_orphan_session_id is None
        assert _requeued_events(f"test-reparks-crash-requeued-{auto}") == [
            _requeue_payload(session)
        ]
    else:
        _assert_left_parked_and_unlinked()
        assert _requeued_events(f"test-reparks-crash-requeued-{auto}") == []


def test_orphan_session_record_gone_clears_linkage_without_side_effects(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing left to close or re-scan: drop the stale link, touch nothing else."""
    _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    save_state(CwState(sessions=[]))
    calls = _spy_process_iter(monkeypatch)

    assert _run(auto=True) == []

    assert calls == []
    _assert_left_parked_and_unlinked()
    assert _completed_events("test-reparks-gone-completed") == []
    assert _requeued_events("test-reparks-gone-requeued") == []
    attention = read_events(
        consumer="test-reparks-gone-attention",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert attention == []


@pytest.mark.parametrize(
    ("resumed_at", "parked_at", "expect_stale"),
    [
        (_NOW, _PARKED_AT, True),
        (_PARKED_AT - timedelta(minutes=5), _PARKED_AT, False),
        (_NOW, None, True),
    ],
)
def test_session_resumed_after_the_park_is_never_closed(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    resumed_at: datetime,
    parked_at: datetime | None,
    expect_stale: bool,
) -> None:
    """``cw resume`` keeps the session id, so a session resumed after the park
    is a live claude process, not the orphan: a codex-writer scan must never
    close it. Only the stale link is cleared. A resume that predates the park
    is part of the orphan's own history and does not stale the link."""
    _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    state = load_state()
    state.sessions[0].resumed_at = resumed_at
    save_state(state)
    store = load_dev_queue()
    store.tasks[0].completed_at = parked_at
    save_dev_queue(store)
    _no_codex_process(monkeypatch)

    assert _run(auto=True) == ([] if expect_stale else [_TICKET])

    if expect_stale:
        _assert_session_active()
        _assert_left_parked_and_unlinked()
        assert _completed_events(f"test-reparks-resumed-{parked_at}") == []
    else:
        _assert_session_closed()
        assert _task().status is QueueItemStatus.PENDING


def test_row_without_linkage_field_is_ignored(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only live-writer/inconclusive-scan parks carry the link; any other
    BLOCKED_ON_USER row is out of scope and never scanned."""
    _seed_parked(tmp_config_dir, tmp_path, make_git_repo, linked=False)
    calls = _spy_process_iter(monkeypatch)

    assert _run(auto=True) == []

    assert calls == []
    _assert_session_active()
    task = _task()
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    assert task.codex_orphan_session_id is None


def test_row_for_an_unknown_client_is_skipped(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client since removed from clients.yaml cannot be policy-resolved."""
    _, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    (tmp_config_dir / ".config" / "cw" / "clients.yaml").write_text("clients: {}\n")
    calls = _spy_process_iter(monkeypatch)

    assert _run(auto=True) == []

    assert calls == []
    _assert_session_active()
    task = _assert_still_parked(session)
    assert task.codex_orphan_rescan_next_eligible_at is None


@pytest.mark.parametrize(
    "disposition",
    [
        None,
        _OrphanDisposition(
            should_requeue=False,
            reason=_PARK_REASON_PROCESS_SCAN_INCONCLUSIVE,
            close_session=False,
        ),
        _OrphanDisposition(
            should_requeue=True, reason=CODEX_ORPHAN_CLEAN_REQUEUE_REASON
        ),
    ],
)
def test_act_skips_a_row_that_changed_since_detect(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    *,
    disposition: _OrphanDisposition | None,
) -> None:
    """Every act arm re-verifies the row under the dev-queue lock: a row the
    operator unblocked since detect (or now linked to another session) is left
    alone, and its session is not closed on the stale decision."""
    _, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    # The row links the live session; the stale candidate names another one.
    candidate = _ReparkCandidate(
        ticket_id=_TICKET,
        client=_CLIENT,
        orphan_session_id="sess-superseded",
        stage=Stage.REVIEW,
        disposition=disposition,
        stale_reason="gone" if disposition is None else None,
    )

    with sessions_lock():
        assert _act_on_live_writer_repark_candidates([candidate], now=_NOW) == []

    _assert_session_active()
    task = _assert_still_parked(session)
    assert task.codex_orphan_rescan_next_eligible_at is None
    assert _completed_events("test-reparks-stale-completed") == []
    assert _requeued_events("test-reparks-stale-requeued") == []


def test_closing_a_session_that_vanished_since_detect_still_decides_the_row(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
) -> None:
    """The session record was removed between detect and act: there is nothing
    to close, so no audit event, but the row's decision still lands."""
    _, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    save_state(CwState(sessions=[]))
    candidate = _ReparkCandidate(
        ticket_id=_TICKET,
        client=_CLIENT,
        orphan_session_id=session.id,
        stage=Stage.REVIEW,
        disposition=_OrphanDisposition(
            should_requeue=False, reason=_PARK_REASON_REAP_POLICY_NOT_AUTO
        ),
    )

    with sessions_lock():
        assert _act_on_live_writer_repark_candidates([candidate], now=_NOW) == []

    assert _completed_events("test-reparks-vanished-completed") == []
    _assert_left_parked_and_unlinked()


def test_dispatch_tick_reconciles_a_live_writer_park(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the real boot pass parks a live-writer orphan and links it;
    the writer then exits and one ordinary reconcile() tick — no second boot —
    closes the session and requeues the row."""
    orchestrator_config_file().parent.mkdir(parents=True, exist_ok=True)
    orchestrator_config_file().write_text("reap_policy: auto\n")
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    # A roster that reports this session live: no daemon-outage short-circuit
    # and no phantom sweep acting on the session ahead of the new sweep.
    state = load_state()
    state.sessions[0].surface_ref = "livesess"
    save_state(state)
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "livesess-0000-0000-0000-000000000000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    _live_writer(monkeypatch, 4242)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    session = _session()
    _assert_session_active()
    parked = _assert_still_parked(session)
    assert parked.codex_orphan_rescan_next_eligible_at is None

    _no_codex_process(monkeypatch)
    reconcile()

    _assert_session_closed()
    task = _task()
    assert task.status is QueueItemStatus.PENDING
    assert task.codex_orphan_session_id is None
    completed = _completed_events("test-reparks-e2e-completed")
    assert [c["reason"] for c in completed] == [CODEX_ORPHAN_CLOSE_REASON_AT_RECONCILE]
    assert _requeued_events("test-reparks-e2e-requeued") == [_requeue_payload(session)]


def test_still_live_writer_rescan_never_repages_the_operator(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rescan never re-parks: across repeated eligible rescans a still-live
    writer emits no fresh SESSION_NEEDS_ATTENTION and no reap proposal, so the
    operator is not re-paged every tick; only the backoff advances."""
    _, session = _seed_parked(tmp_config_dir, tmp_path, make_git_repo)
    _live_writer(monkeypatch, 4242)
    later = _NOW + timedelta(seconds=_LIVE_WRITER_RESCAN_BACKOFF_SECONDS)

    _run(auto=True)
    _run(auto=True, now=later)

    for event_type in (
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        OrchestratorEventType.SESSION_REAP_PROPOSED,
    ):
        assert (
            read_events(
                consumer=f"test-reparks-repage-{event_type}", event_types=[event_type]
            )
            == []
        )
    task = _assert_still_parked(session)
    assert task.codex_orphan_rescan_next_eligible_at == later + timedelta(
        seconds=_LIVE_WRITER_RESCAN_BACKOFF_SECONDS
    )
