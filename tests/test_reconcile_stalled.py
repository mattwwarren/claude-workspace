"""Unit tests for cw.reconcile.stalled.

Wall-clock-budget stalled-headless sweep: revert/timeout, branch-state
tagging, SESSION_STAGE_TIMED_OUT_RETRIED events, retry-cap, worktree cleanup,
sentinel-salvage, and detect/act candidate tests.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import freezegun
import pytest

from cw._util import claude_project_dir
from cw.auto_dev_result import (
    AutoDevResult,
)
from cw.config import (
    load_state,
    save_state,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import WorktreeError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    _NEEDS_SALVAGE_REASON,
    _SILENTLY_IDLE_REASON,
    HEADLESS_TIMEOUT_SECONDS,
    UsageLimitDetection,
    reconcile,
    resolve_headless_budget,
    revert_stalled_headless_sessions,
)
from tests._reconcile_helpers import (
    _auto_config,
    _mk_headless_daemon_session,
    _mk_session,
    _no_op_salvage_payload,
    _shipped_salvage_payload,
    _state_queue_snapshot,
    _ul_record,
    _write_idle_transcript_with_text,
    _write_salvage_transcript,
    _write_staged_clients_yaml,
    _write_transcript_records,
)


def test_revert_stalled_headless_sessions_transitions_past_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session past budget → TIMED_OUT, task reverted, event emitted."""
    worktree = tmp_path / "wt-stalled"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("stalled-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="stalled-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="stalled-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "stalled-1" in reverted
    assert sess.status == SessionStatus.TIMED_OUT
    assert sess.completed_reason == CompletionReason.TIMED_OUT
    assert sess.completed_at == now

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "stalled-1")
    assert s.status == SessionStatus.TIMED_OUT

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "stalled-1")
    assert t.status == QueueItemStatus.PENDING
    assert t.session_id is None

    events = read_events(
        consumer="test-stalled-1",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["session_id"] == "stalled-1"
    assert payload["elapsed_seconds"] >= HEADLESS_TIMEOUT_SECONDS


def test_revert_stalled_headless_sessions_leaves_under_budget_alone(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session under budget → unchanged."""
    worktree = tmp_path / "wt-under"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() < HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("under-budget", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert reverted == []
    assert sess.status == SessionStatus.ACTIVE


def test_revert_stalled_headless_sessions_catches_idle_sessions(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """IDLE headless DAEMON session past budget → TIMED_OUT (not ACTIVE-only)."""
    worktree = tmp_path / "wt-idle"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("idle-stalled", worktree, started_at)
    sess.status = SessionStatus.IDLE
    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert reverted == []  # no matching ticket task
    assert sess.status == SessionStatus.TIMED_OUT


def test_revert_stalled_headless_sessions_skips_non_daemon(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """USER-origin session past budget → unchanged."""
    worktree = tmp_path / "wt-user"
    worktree.mkdir(parents=True, exist_ok=True)
    context_dir = worktree / ".claude"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "cw-context.json").write_text('{"headless": true}')

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = Session(
        id="user-sess",
        name="client-a/user-sess",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.USER,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert reverted == []
    assert sess.status == SessionStatus.ACTIVE


def test_revert_stalled_headless_sessions_fail_open_missing_context(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session with no cw-context.json → fail-open, not transitioned."""
    worktree = tmp_path / "wt-nocontext"
    worktree.mkdir(parents=True, exist_ok=True)
    # Deliberately do NOT write cw-context.json

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = Session(
        id="no-ctx",
        name="client-a/auto-dev/no-ctx",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert reverted == []
    assert sess.status == SessionStatus.ACTIVE


def test_revert_stalled_headless_sessions_skips_none_worktree_path(
    tmp_config_dir: Path,
) -> None:
    """DAEMON session with worktree_path=None → treated as not headless."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_session("no-wt", surface_ref="some-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.started_at = started_at
    assert sess.worktree_path is None

    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert reverted == []
    assert sess.status == SessionStatus.ACTIVE


def test_revert_stalled_headless_sessions_stops_daemon_surface(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stalled session has surface_ref → get_native_daemon_client().stop() called."""
    worktree = tmp_path / "wt-stop"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    daemon = FakeNativeDaemonClient()
    short_id = daemon.spawn_bg(cwd=tmp_path, prompt="seed")
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)

    sess = _mk_headless_daemon_session(
        "stop-me", worktree, started_at, surface_ref=short_id
    )
    state = CwState(sessions=[sess])
    save_state(state)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert short_id in daemon.stop_calls


def test_revert_stalled_headless_sessions_merged_pr_completes_not_times_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315: stalled session whose PR is merged → COMPLETED, not TIMED_OUT."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-315-merged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("315-merged", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="315-merged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-merged",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (True, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "315-merged" not in reverted
    assert sess.status == SessionStatus.COMPLETED
    assert sess.completed_reason == CompletionReason.NORMAL
    assert sess.reap_reason == ReapReason.WALL_CLOCK_BUDGET

    store = load_dev_queue()
    task_after = next(t for t in store.tasks if t.ticket_id == "315-merged")
    assert task_after.status == QueueItemStatus.COMPLETED
    assert task_after.session_id is None

    completed_events = read_events(
        consumer="test-315-merged-completed",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert any(e.payload.get("session_id") == sess.id for e in completed_events)

    timed_out_events = read_events(
        consumer="test-315-merged-no-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert not any(e.payload.get("session_id") == sess.id for e in timed_out_events)


def test_revert_stalled_headless_sessions_not_merged_times_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315: stalled session whose PR is not merged → TIMED_OUT unchanged."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-315-notmerged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    sess = _mk_headless_daemon_session("315-notmerged", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="315-notmerged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-notmerged",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    branch_cwds: list[object] = []

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    def _capture_branch_exists(_branch: str, **kw: object) -> tuple[bool | None, bool]:
        branch_cwds.append(kw.get("cwd"))
        return True, True

    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin", _capture_branch_exists
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "315-notmerged" in reverted
    assert sess.status == SessionStatus.TIMED_OUT
    # branch_exists_on_origin scoped to client-a's repo cwd (#1279).
    assert branch_cwds == [Path("/tmp/ws-staged")]

    store = load_dev_queue()
    task_after = next(t for t in store.tasks if t.ticket_id == "315-notmerged")
    assert task_after.status == QueueItemStatus.PENDING

    timed_out_events = read_events(
        consumer="test-315-notmerged-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert any(e.payload.get("session_id") == sess.id for e in timed_out_events)

    completed_events = read_events(
        consumer="test-315-notmerged-no-completed",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert not any(e.payload.get("session_id") == sess.id for e in completed_events)


def test_revert_stalled_headless_sessions_dangling_client_skips_gh_blocks_user(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1279 R7: a REVERT_TASK candidate whose client is absent from a populated
    clients.yaml skips BOTH gh calls (pr_is_merged_for_ticket and
    branch_exists_on_origin) and routes the ticket to BLOCKED_ON_USER."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-1279-dangling"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    # clients.yaml populated with a DIFFERENT client than the session's
    # (client-a) → client-a is dangling/config-drifted.
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n"
        "  client-b:\n"
        "    workspace_path: /tmp/ws-other\n"
        "    default_branch: main\n"
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
    )
    monkeypatch.setattr("cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None)

    sess = _mk_headless_daemon_session("1279-dangling", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="1279-dangling",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1279-dangling",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    merged_calls: list[str] = []
    branch_calls: list[str] = []

    def _capture_merged(tid: str, **_kw: object) -> tuple[bool | None, bool]:
        merged_calls.append(tid)
        return True, True

    def _capture_branch(branch: str, **_kw: object) -> tuple[bool | None, bool]:
        branch_calls.append(branch)
        return True, True

    monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture_merged)
    monkeypatch.setattr("cw.reconcile._deps.branch_exists_on_origin", _capture_branch)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # Neither gh call fires for the dangling client (#1279 R7).
    assert merged_calls == []
    assert branch_calls == []

    store = load_dev_queue()
    task_after = next(t for t in store.tasks if t.ticket_id == "1279-dangling")
    assert task_after.status == QueueItemStatus.BLOCKED_ON_USER


def test_revert_stalled_headless_sessions_transient_gh_error_times_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315: transient gh error (None, True) → TIMED_OUT — fail-open behavior."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-315-gherror"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("315-gherror", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="315-gherror",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-gherror",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (None, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "315-gherror" in reverted
    assert sess.status == SessionStatus.TIMED_OUT

    timed_out_events = read_events(
        consumer="test-315-gherror-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert any(e.payload.get("session_id") == sess.id for e in timed_out_events)


def test_branch_absent_no_merged_pr_tags_session_timed_out_event(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (a): no merged PR + branch absent → TIMED_OUT, PENDING, branch_state."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-absent"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-absent", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-absent",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-absent",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (False, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # Security assertion: branch-absent MUST NOT complete the session.
    assert "808-absent" in reverted
    assert sess.status == SessionStatus.TIMED_OUT
    assert sess.completed_reason == CompletionReason.TIMED_OUT

    store = load_dev_queue()
    task_after = next(t for t in store.tasks if t.ticket_id == "808-absent")
    assert task_after.status == QueueItemStatus.PENDING

    timed_out_events = read_events(
        consumer="test-808-absent-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    matching = [e for e in timed_out_events if e.payload.get("session_id") == sess.id]
    assert len(matching) == 1
    assert matching[0].payload.get("branch_state") == "absent_no_merged_pr"

    completed_events = read_events(
        consumer="test-808-absent-no-completed",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert not any(e.payload.get("session_id") == sess.id for e in completed_events)


def test_branch_present_omits_branch_state_key(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (b): no merged PR + branch present → TIMED_OUT, branch_state key absent."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-present"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-present", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-present",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-present",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "808-present" in reverted
    assert sess.status == SessionStatus.TIMED_OUT

    timed_out_events = read_events(
        consumer="test-808-present-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    matching = [e for e in timed_out_events if e.payload.get("session_id") == sess.id]
    assert len(matching) == 1
    assert "branch_state" not in matching[0].payload


def test_branch_check_transient_error_omits_branch_state(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (c): branch-check error (None, True) → TIMED_OUT, branch_state omitted."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-transient"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-transient", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-transient",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-transient",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (None, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "808-transient" in reverted
    assert sess.status == SessionStatus.TIMED_OUT

    timed_out_events = read_events(
        consumer="test-808-transient-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    matching = [e for e in timed_out_events if e.payload.get("session_id") == sess.id]
    assert len(matching) == 1
    assert "branch_state" not in matching[0].payload


def test_branch_check_not_called_when_pr_merged(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (d): merged PR → COMPLETED; branch_exists_on_origin never called."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-merged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-merged", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-merged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-merged",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (True, True),
    )

    branch_check_count = 0

    def _branch_check_forbidden(_branch: str, **_kw: object) -> tuple[bool, bool]:
        nonlocal branch_check_count
        branch_check_count += 1
        return (True, True)

    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        _branch_check_forbidden,
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert branch_check_count == 0
    assert sess.status == SessionStatus.COMPLETED


def test_branch_check_not_called_on_transient_pr_error(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (e): transient PR error (None, True) → TIMED_OUT; branch check skipped."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-pr-transient"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-pr-transient", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-pr-transient",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-pr-transient",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (None, True),
    )

    branch_check_count = 0

    def _branch_check_forbidden(_branch: str, **_kw: object) -> tuple[bool, bool]:
        nonlocal branch_check_count
        branch_check_count += 1
        return (True, True)

    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        _branch_check_forbidden,
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert branch_check_count == 0
    assert "808-pr-transient" in reverted
    assert sess.status == SessionStatus.TIMED_OUT


def test_revert_stalled_gh_prepass_skips_none_ticket_id_and_none_client(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315 pre-pass guard branches: ticket_id=None and client=None are skipped.

    Covers lines 512 (continue when ticket_id is None) and 514 (continue when
    client is None).  Neither candidate should reach pr_is_merged_for_ticket.

    - Session A: name "client-a/impl" → ticket_id_for_session returns None
      → candidate.ticket_id is None → line 512 continue.
    - Session B: normal auto-dev name with valid ticket, but _detect_stalled_candidates
      is patched to also emit a REVERT_TASK candidate with client=None to hit line 514.
    """
    from cw.reconcile import ProposedAction, ReapCandidate
    from cw.reconcile.stalled import _detect_stalled_candidates as _real_detect

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    # Session A — non-auto-dev name → ticket_id will be None in the real candidate.
    worktree_a = tmp_path / "wt-none-tid"
    worktree_a.mkdir(parents=True, exist_ok=True)
    (worktree_a / ".claude").mkdir(parents=True, exist_ok=True)
    (worktree_a / ".claude" / "cw-context.json").write_text(
        '{"headless": true, "session_id": "none-tid"}'
    )
    sess_a = Session(
        id="none-tid",
        name="client-a/impl",  # No "auto-dev/" prefix → ticket_id=None
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree_a,
        started_at=started_at,
    )
    state = CwState(sessions=[sess_a])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    # Wrap real detect to also inject a candidate with client=None (covers line 514).
    def _patched_detect(
        s: object,
        *,
        now: datetime,
        config: object,
        task_by_ticket: object,
    ) -> list[ReapCandidate]:
        real_candidates: list[ReapCandidate] = _real_detect(
            s,  # type: ignore[arg-type]
            now=now,
            config=config,  # type: ignore[arg-type]
            task_by_ticket=task_by_ticket,  # type: ignore[arg-type]
        )
        # Inject a REVERT_TASK candidate with a valid ticket_id but client=None.
        # Re-use sess_a's session_id so the act phase can resolve the session.
        # The candidate is skipped in the gh pre-pass (line 514: client is None),
        # then falls through to the normal TIMED_OUT revert path (no queue task
        # to update, so the queue remains unchanged).
        injected = ReapCandidate(
            session_id="none-tid",  # matches sess_a.id so act phase succeeds
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="TKT-NONE-CLIENT",
            client=None,  # Line 514: continue (skipped in gh pre-pass)
        )
        return [*real_candidates, injected]

    monkeypatch.setattr(
        "cw.reconcile.stalled.core._detect_stalled_candidates",
        _patched_detect,
    )

    # pr_is_merged_for_ticket must NOT be called for either skipped candidate.
    call_count = 0

    def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool, bool]:
        nonlocal call_count
        call_count += 1
        return (False, True)

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        _should_not_be_called,
    )

    # Act — neither skipped candidate should trigger a gh call.
    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # Assert — gh was never called (both candidates were skipped before the call).
    assert call_count == 0


def test_revert_stalled_gh_prepass_second_candidate_skips_when_gh_gone(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315 pre-pass: gh goes unavailable on first candidate → second short-circuits.

    Covers lines 523-525 (gh becomes unavailable for the first REVERT_TASK
    candidate: _merged, False return → set _gh_available=False, append to
    gh_blocked, continue) and lines 516-517 (second candidate sees
    _gh_available=False → append to gh_blocked, continue without calling gh).

    Both sessions end up in BLOCKED_ON_USER state via the gh_blocked routing.
    """
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    worktree_b = tmp_path / "wt-gh-gone-b"
    worktree_c = tmp_path / "wt-gh-gone-c"
    sess_b = _mk_headless_daemon_session("315-gh-gone-b", worktree_b, started_at)
    sess_c = _mk_headless_daemon_session("315-gh-gone-c", worktree_c, started_at)
    state = CwState(sessions=[sess_b, sess_c])
    save_state(state)

    task_b = TicketTask(
        ticket_id="315-gh-gone-b",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-gh-gone-b",
    )
    task_c = TicketTask(
        ticket_id="315-gh-gone-c",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-gh-gone-c",
    )
    save_dev_queue(DevQueueStore(tasks=[task_b, task_c]))

    # First call → gh unavailable (None, False); second call must not happen.
    gh_call_count = 0

    def _first_call_unavailable(_tid: str, **_kw: object) -> tuple[None, bool]:
        nonlocal gh_call_count
        gh_call_count += 1
        return (None, False)  # _gh_avail=False → triggers lines 522-525

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        _first_call_unavailable,
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # gh was called exactly once — second candidate hit the short-circuit (516-517).
    assert gh_call_count == 1
    # Neither session was reverted (both blocked on gh unavailability).
    assert "315-gh-gone-b" not in reverted
    assert "315-gh-gone-c" not in reverted
    # Both tasks remain BLOCKED_ON_USER (gh routing).
    store = load_dev_queue()
    task_b_after = next(t for t in store.tasks if t.ticket_id == "315-gh-gone-b")
    task_c_after = next(t for t in store.tasks if t.ticket_id == "315-gh-gone-c")
    assert task_b_after.status == QueueItemStatus.BLOCKED_ON_USER
    assert task_c_after.status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# SESSION_STAGE_TIMED_OUT_RETRIED event (GitHub issue #724)
# ---------------------------------------------------------------------------


def test_session_stage_timed_out_retried_event_emitted_auto_policy(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto policy: genuine timeout emits SESSION_STAGE_TIMED_OUT_RETRIED."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-auto"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("retried-auto", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="retried-auto",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="retried-auto",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-retried-auto",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ticket_id"] == "retried-auto"
    assert payload["session_id"] == "retried-auto"
    assert payload["stage"] == Stage.PLAN
    assert payload["client"] == "client-a"
    assert payload["elapsed_seconds"] >= HEADLESS_TIMEOUT_SECONDS
    assert payload["attempts"] == 1


def test_session_stage_timed_out_retried_event_emitted_signal_only_policy(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signal-only: SESSION_STAGE_TIMED_OUT_RETRIED fires before policy routing."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-signal"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("retried-signal", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="retried-signal",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="retried-signal",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )

    # signal_only is the default OrchestratorConfig
    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-retried-signal",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert len(events) == 1
    assert events[0].payload["ticket_id"] == "retried-signal"


def test_session_stage_timed_out_retried_not_emitted_for_merged_pr(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merged PR: SESSION_STAGE_TIMED_OUT_RETRIED is NOT emitted."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-merged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("retried-merged", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="retried-merged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="retried-merged",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (True, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-retried-merged",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert not any(e.payload.get("ticket_id") == "retried-merged" for e in events)


def test_session_stage_timed_out_retried_not_emitted_for_gh_blocked(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GH-blocked candidate: SESSION_STAGE_TIMED_OUT_RETRIED is NOT emitted."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-gh-blocked"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("retried-gh-blocked", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="retried-gh-blocked",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="retried-gh-blocked",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (None, False),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-retried-gh-blocked",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert not any(e.payload.get("ticket_id") == "retried-gh-blocked" for e in events)


# ---------------------------------------------------------------------------
# SESSION_STAGE_TIMED_OUT_RETRIED edge-trigger — fires once, suppressed on
# re-detect (GitHub #782, Source A)
# ---------------------------------------------------------------------------


def test_timed_out_retried_not_reemitted_on_second_detect_signal_only(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signal-only keeps session live → TIMED_OUT_RETRIED must fire exactly once."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-storm-a"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("storm-a", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="storm-a",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="storm-a",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    # Tick 1: event fires, reap_proposed_at stamped and persisted.
    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    # Tick 2: reload from disk (mirrors _reconcile_locked production flow) to
    # verify suppression holds across the persistence boundary, not just in-memory.
    state = load_state()
    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-storm-a",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    matching = [e for e in events if e.payload.get("ticket_id") == "storm-a"]
    assert len(matching) == 1, (
        f"Expected exactly 1 TIMED_OUT_RETRIED but got {len(matching)}"
    )


def test_timed_out_retried_suppressed_when_already_proposed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-stamped reap_proposed_at suppresses TIMED_OUT_RETRIED on that session."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-storm-b"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("storm-b", worktree, started_at)
    # Pre-stamp reap_proposed_at to simulate a prior tick having already proposed.
    sess.reap_proposed_at = datetime(2026, 1, 1, 0, 30, 0, tzinfo=UTC)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="storm-b",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="storm-b",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-storm-b",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert not any(e.payload.get("ticket_id") == "storm-b" for e in events), (
        "TIMED_OUT_RETRIED must not fire when session already had reap_proposed_at"
    )


def test_timed_out_retried_fires_for_new_session_suppressed_for_existing(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed: new session fires TIMED_OUT_RETRIED; already-proposed suppressed."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree_new = tmp_path / "wt-storm-new"
    worktree_old = tmp_path / "wt-storm-old"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess_new = _mk_headless_daemon_session("storm-new", worktree_new, started_at)
    sess_old = _mk_headless_daemon_session("storm-old", worktree_old, started_at)
    sess_old.reap_proposed_at = datetime(2026, 1, 1, 0, 30, 0, tzinfo=UTC)

    state = CwState(sessions=[sess_new, sess_old])
    save_state(state)

    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="storm-new",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="storm-new",
                    stage=Stage.PLAN,
                    attempts=1,
                ),
                TicketTask(
                    ticket_id="storm-old",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="storm-old",
                    stage=Stage.PLAN,
                    attempts=1,
                ),
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-storm-mixed",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    fired_tids = {e.payload.get("ticket_id") for e in events}
    assert "storm-new" in fired_tids, "New session must fire TIMED_OUT_RETRIED"
    assert "storm-old" not in fired_tids, (
        "Already-proposed session must NOT fire TIMED_OUT_RETRIED"
    )


# ---------------------------------------------------------------------------
# resolve_stalled_retry_cap + stalled_retry_cap_by_tier config field (#756)
# ---------------------------------------------------------------------------


def test_resolve_stalled_retry_cap_default_with_no_task() -> None:
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, resolve_stalled_retry_cap

    assert resolve_stalled_retry_cap(None, _auto_config()) == DEFAULT_STALLED_RETRY_CAP


def test_resolve_stalled_retry_cap_respects_per_tier() -> None:
    from cw.reconcile import resolve_stalled_retry_cap

    cfg = _auto_config(stalled_retry_cap_by_tier={"large": 5})
    task = TicketTask(ticket_id="T", client="c", scope_hint="large")
    assert resolve_stalled_retry_cap(task, cfg) == 5


def test_resolve_stalled_retry_cap_unknown_tier_falls_back() -> None:
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, resolve_stalled_retry_cap

    cfg = _auto_config(stalled_retry_cap_by_tier={"large": 5})
    task = TicketTask(ticket_id="T", client="c", scope_hint="small")
    assert resolve_stalled_retry_cap(task, cfg) == DEFAULT_STALLED_RETRY_CAP


def test_stalled_retry_cap_reverts_below_cap(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempts < DEFAULT_STALLED_RETRY_CAP → normal REVERT_TASK path (PENDING)."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-below-cap"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("below-cap", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="below-cap",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="below-cap",
        attempts=DEFAULT_STALLED_RETRY_CAP - 1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "below-cap" in reverted
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "below-cap")
    assert t.status == QueueItemStatus.PENDING

    # Regression guard for #724: SESSION_STAGE_TIMED_OUT_RETRIED must still fire
    # on the below-cap path (session is being retried, not parked).
    events = read_events(
        consumer="test-below-cap-retried",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert any(e.payload.get("ticket_id") == "below-cap" for e in events)


def test_stalled_retry_cap_parks_when_at_cap(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempts >= cap → BLOCKED_ON_USER, SESSION_NEEDS_ATTENTION emitted (#756)."""
    from cw.reconcile import (
        _STALLED_CAP_PARKED_REASON,
        DEFAULT_STALLED_RETRY_CAP,
        HEADLESS_TIMEOUT_SECONDS,
    )

    worktree = tmp_path / "wt-at-cap"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("at-cap", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="at-cap",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="at-cap",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
        lane="stalled-park-lane",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    reverted = revert_stalled_headless_sessions(
        state, now=now, config=_auto_config(headless_timeout_by_stage={})
    )

    assert "at-cap" not in reverted

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "at-cap")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER
    assert t.session_id is None
    assert t.disposition == _STALLED_CAP_PARKED_REASON

    s = next(s for s in state.sessions if s.id == "at-cap")
    assert s.status == SessionStatus.TIMED_OUT
    assert s.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED

    events = read_events(
        consumer="test-at-cap",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ticket_id"] == "at-cap"
    assert payload["paused_status"] == _STALLED_CAP_PARKED_REASON
    assert payload["lane"] == "stalled-park-lane"


def test_stalled_retry_cap_no_retried_event_when_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parked by retry cap: SESSION_STAGE_TIMED_OUT_RETRIED must NOT fire (#756)."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-cap-no-retry-evt"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("cap-no-evt", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cap-no-evt",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cap-no-evt",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-cap-no-evt",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert not any(e.payload.get("ticket_id") == "cap-no-evt" for e in events)


def test_stalled_revert_usage_limit_detected_sets_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030: wall-clock REVERT_TASK path threads usage_limit_detected onto
    its ReapCandidate → reap_reason=usage_limit_cutoff when the session's
    transcript shows a usage-limit refusal."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-revert-ul"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("revert-ul", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="revert-ul",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="revert-ul",
        attempts=1,  # below cap -> normal REVERT_TASK path
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )
    with patch(
        "cw.reconcile._shared.detect_usage_limit",
        return_value=UsageLimitDetection(
            detected=True, matched_at=None, transcript_tail_at=None
        ),
    ):
        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config()
        )

    assert "revert-ul" in reverted
    s = next(s for s in state.sessions if s.id == "revert-ul")
    assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF


def test_stalled_revert_no_usage_limit_keeps_wall_clock_budget_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030: revert path's False arm — no usage-limit transcript keeps the
    original wall_clock_budget cause."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-revert-no-ul"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("revert-no-ul", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="revert-no-ul",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="revert-no-ul",
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )
    with patch(
        "cw.reconcile._shared.detect_usage_limit",
        return_value=UsageLimitDetection(
            detected=False, matched_at=None, transcript_tail_at=None
        ),
    ):
        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config()
        )

    assert "revert-no-ul" in reverted
    s = next(s for s in state.sessions if s.id == "revert-no-ul")
    assert s.reap_reason == ReapReason.WALL_CLOCK_BUDGET


def test_stalled_cap_park_usage_limit_detected_sets_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030: the cap-park branch is NEW usage-limit logic (unlike the revert
    path, which mirrors idle.py precedent) — direct coverage required.
    attempts >= cap + usage-limit transcript →
    reap_reason=usage_limit_cutoff instead of stalled_retry_cap_parked."""
    from cw.reconcile import (
        _STALLED_CAP_PARKED_REASON,
        DEFAULT_STALLED_RETRY_CAP,
        HEADLESS_TIMEOUT_SECONDS,
    )

    worktree = tmp_path / "wt-cap-ul"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("cap-ul", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cap-ul",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cap-ul",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    with patch(
        "cw.reconcile._shared.detect_usage_limit",
        return_value=UsageLimitDetection(
            detected=True, matched_at=None, transcript_tail_at=None
        ),
    ):
        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config(headless_timeout_by_stage={})
        )

    assert "cap-ul" not in reverted  # still a park, not a requeue
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "cap-ul")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER
    # Task-level disposition is unaffected — only session.reap_reason changes.
    assert t.disposition == _STALLED_CAP_PARKED_REASON

    s = next(s for s in state.sessions if s.id == "cap-ul")
    assert s.status == SessionStatus.TIMED_OUT
    assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF


def test_stalled_cap_park_no_usage_limit_keeps_retry_cap_parked_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030: cap-park branch's False arm — no usage-limit transcript keeps
    the original stalled_retry_cap_parked cause."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-cap-no-ul"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("cap-no-ul", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cap-no-ul",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cap-no-ul",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    with patch(
        "cw.reconcile._shared.detect_usage_limit",
        return_value=UsageLimitDetection(
            detected=False, matched_at=None, transcript_tail_at=None
        ),
    ):
        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config(headless_timeout_by_stage={})
        )

    assert "cap-no-ul" not in reverted
    s = next(s for s in state.sessions if s.id == "cap-no-ul")
    assert s.status == SessionStatus.TIMED_OUT
    assert s.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED


_STALLED_UL_RECENT = [
    _ul_record("working the ticket", "2026-01-01T00:00:10+00:00"),
    _ul_record(
        "You've hit your session limit · resets 11am", "2026-01-01T00:00:20+00:00"
    ),
]
# Early limit the worker recovered from, then unrelated work 301s later (stale).
_STALLED_UL_STALE = [
    _ul_record(
        "You've hit your session limit · resets 11am", "2026-01-01T00:00:10+00:00"
    ),
    _ul_record("unrelated later progress", "2026-01-01T00:05:11+00:00"),
]


def _setup_stalled_real_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sid: str,
    records: list[dict[str, object]],
    attempts: int,
) -> tuple[CwState, datetime]:
    """Build a wall-clock-expired stalled session with a REAL timestamped
    transcript (no detect_usage_limit mock). Returns (state, now)."""
    home = tmp_path / f"home-{sid}"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / f"wt-{sid}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session(sid, worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=sid,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=sid,
                    stage=Stage.IMPL,
                    attempts=attempts,
                )
            ]
        )
    )
    transcript = _write_transcript_records(home, worktree, records)
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )
    return state, now


def test_stalled_revert_usage_limit_recent_sets_cutoff_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345 revert path, real detector: limit at tail → USAGE_LIMIT_CUTOFF."""
    state, now = _setup_stalled_real_transcript(
        tmp_path,
        monkeypatch,
        sid="revert-ul-recent",
        records=_STALLED_UL_RECENT,
        attempts=1,
    )
    reverted = revert_stalled_headless_sessions(
        state, now=now, config=_auto_config(headless_timeout_by_stage={})
    )
    assert "revert-ul-recent" in reverted
    s = next(s for s in state.sessions if s.id == "revert-ul-recent")
    assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF


def test_stalled_revert_usage_limit_stale_keeps_wall_clock_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345 revert path, real detector: stale limit → WALL_CLOCK_BUDGET."""
    state, now = _setup_stalled_real_transcript(
        tmp_path,
        monkeypatch,
        sid="revert-ul-stale",
        records=_STALLED_UL_STALE,
        attempts=1,
    )
    reverted = revert_stalled_headless_sessions(
        state, now=now, config=_auto_config(headless_timeout_by_stage={})
    )
    assert "revert-ul-stale" in reverted
    s = next(s for s in state.sessions if s.id == "revert-ul-stale")
    assert s.reap_reason == ReapReason.WALL_CLOCK_BUDGET


def test_stalled_cap_park_usage_limit_recent_sets_cutoff_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345 cap-park branch, real detector: limit at tail → USAGE_LIMIT_CUTOFF."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP

    state, now = _setup_stalled_real_transcript(
        tmp_path,
        monkeypatch,
        sid="cap-ul-recent",
        records=_STALLED_UL_RECENT,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    reverted = revert_stalled_headless_sessions(
        state, now=now, config=_auto_config(headless_timeout_by_stage={})
    )
    assert "cap-ul-recent" not in reverted  # park, not requeue
    s = next(s for s in state.sessions if s.id == "cap-ul-recent")
    assert s.status == SessionStatus.TIMED_OUT
    assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF


def test_stalled_cap_park_usage_limit_stale_keeps_retry_cap_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345 cap-park branch, real detector: stale limit → STALLED_RETRY_CAP_PARKED."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP

    state, now = _setup_stalled_real_transcript(
        tmp_path,
        monkeypatch,
        sid="cap-ul-stale",
        records=_STALLED_UL_STALE,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    reverted = revert_stalled_headless_sessions(
        state, now=now, config=_auto_config(headless_timeout_by_stage={})
    )
    assert "cap-ul-stale" not in reverted
    s = next(s for s in state.sessions if s.id == "cap-ul-stale")
    assert s.status == SessionStatus.TIMED_OUT
    assert s.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED


def test_reconcile_usage_limited_true_from_stalled_wall_clock_path(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030 root cause: reconcile()'s watchdog_usage_limited aggregation
    previously snapshotted pre_watchdog_timed_out_ids AFTER the stalled sweep
    already ran, so a usage-limit death caught by the stalled sweep's
    REVERT_TASK path (not just idle/phantom) could never reach
    ReconcileReport.usage_limited. This is the exact incident path: wall-clock
    budget expiry on a session that died to the account session limit."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-stalled-ul-reconcile"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    surface_ref = "stall-ul1"
    sess = _mk_headless_daemon_session(
        "stalled-ul-reconcile", worktree, started_at, surface_ref=surface_ref
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="stalled-ul-reconcile",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="stalled-ul-reconcile",
                    attempts=1,  # below cap -> normal REVERT_TASK path
                )
            ]
        )
    )

    transcript = _write_idle_transcript_with_text(
        home,
        worktree,
        "You've hit your session limit · resets 11:50am ET",
        filename=f"{surface_ref}-sess-1030.jsonl",
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )
    # Non-empty (decoy) live set bypasses the outage guard.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy00001111222233334444555566"}],
    )

    report = reconcile()

    assert report.usage_limited is True
    assert "stalled-ul-reconcile" in report.reverted_ticket_ids


def test_stalled_retry_cap_per_tier_override(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stalled_retry_cap_by_tier overrides default — large-tier cap=1 parks after 1."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-tier-cap"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("tier-cap", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="tier-cap",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="tier-cap",
        scope_hint="large",
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    cfg = _auto_config(stalled_retry_cap_by_tier={"large": 1})
    reverted = revert_stalled_headless_sessions(state, now=now, config=cfg)

    assert "tier-cap" not in reverted
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "tier-cap")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# Stale-worktree cleanup on timeout (GitHub issue #404): a timed-out session's
# task is reverted to PENDING, so its worktree must be removed or the retry
# would inherit this run's branch/commits.
# ---------------------------------------------------------------------------


def test_revert_stalled_cleans_up_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wall-clock timeout removes the stale worktree so re-dispatch is clean."""
    worktree = tmp_path / "wt-cleanup"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session(
        "clean-1", worktree, started_at, surface_ref=None
    )
    sess.branch = "auto-dev/clean-1"
    state = CwState(sessions=[sess])
    save_state(state)

    removed: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.remove_worktree",
        lambda client, branch, *, force=False: removed.append(
            (client.name, branch, force)
        ),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert removed == [("client-a", "auto-dev/clean-1", True)]


def test_revert_stalled_worktree_cleanup_is_best_effort(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worktree-removal failure must not abort the timeout sweep (#404)."""
    worktree = tmp_path / "wt-boom"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("boom-1", worktree, started_at, surface_ref=None)
    sess.branch = "auto-dev/boom-1"
    state = CwState(sessions=[sess])
    save_state(state)

    def boom(client: ClientConfig, branch: str, *, force: bool = False) -> None:
        msg = "git worktree remove exploded"
        raise WorktreeError(msg)

    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr("cw.reconcile._shared.remove_worktree", boom)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert sess.status == SessionStatus.TIMED_OUT


def test_revert_stalled_skips_cleanup_when_no_branch(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with no branch attempts no worktree cleanup (#404)."""
    worktree = tmp_path / "wt-nobranch"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session(
        "nobranch-1", worktree, started_at, surface_ref=None
    )
    # branch left as the model default (None)
    state = CwState(sessions=[sess])
    save_state(state)

    calls: list[str] = []

    def record_get_client(name: str) -> ClientConfig:
        calls.append(name)
        return ClientConfig(name=name, workspace_path=tmp_path / "ws")

    monkeypatch.setattr("cw.reconcile._shared.get_client", record_get_client)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert calls == []


# ---------------------------------------------------------------------------
# Dirty-check guard on worktree cleanup (GitHub issue #425): force-remove must
# be skipped when the worktree has unsaved work; task parks as BLOCKED_ON_USER.
# ---------------------------------------------------------------------------


def test_revert_stalled_skips_removal_and_blocks_task_when_dirty(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timed-out session with unpushed commits: worktree NOT removed.

    Task must move to BLOCKED_ON_USER, not PENDING (#425).
    """
    worktree = tmp_path / "wt-dirty"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session(
        "dirty-1", worktree, started_at, surface_ref=None
    )
    sess.branch = "auto-dev/dirty-1"
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="dirty-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="dirty-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    removed: list[str] = []
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.remove_worktree",
        lambda _client, branch, *, _force=False: removed.append(branch),
    )
    # Simulate dirty worktree (has unsaved work)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # Worktree must NOT have been removed
    assert removed == []
    # Task must be BLOCKED_ON_USER (not PENDING)
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "dirty-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


def test_revert_stalled_removes_when_clean(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timed-out session with clean worktree: removal proceeds as before."""
    worktree = tmp_path / "wt-cleanX"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session(
        "cleanX-1", worktree, started_at, surface_ref=None
    )
    sess.branch = "auto-dev/cleanX-1"
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cleanX-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cleanX-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    removed: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.remove_worktree",
        lambda client, branch, *, force=False: removed.append(
            (client.name, branch, force)
        ),
    )
    # Clean worktree
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # Removal proceeds with force=True
    assert removed == [("client-a", "auto-dev/cleanX-1", True)]
    # Task reverted to PENDING (normal timeout path)
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "cleanX-1")
    assert t.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# Sentinel-salvage tests (GitHub issue #372): a stalled/crashed session that
# emitted a terminal-success sentinel must be dispositioned by that sentinel,
# not mislabeled timed_out/crash, and its ticket must NOT be re-dispatched.
# ---------------------------------------------------------------------------


def test_revert_stalled_salvages_shipped_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-budget session that shipped → COMPLETED, task NOT reverted."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-salv"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-1", worktree, started_at)
    _write_salvage_transcript(
        home, worktree, "claude-uuid-1", _shipped_salvage_payload()
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-1",
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=_auto_config()
    )

    # Not reverted for re-dispatch.
    assert reverted == []
    reloaded = next(s for s in load_state().sessions if s.id == "salv-1")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "shipped"
    assert reloaded.claude_session_id == "fake-short-id-claude-uuid-1"
    assert reloaded.cost_usd == 1.5

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-1")
    assert task.status == QueueItemStatus.COMPLETED

    events = read_events(
        consumer="test-salv-1",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    payload = next(e.payload for e in events if e.payload.get("ticket_id") == "salv-1")
    assert payload["crashed"] is False
    assert payload["salvaged"] is True


def test_revert_stalled_salvages_no_op_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-budget session that no-op'd → COMPLETED, task NOT reverted."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-noop"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-noop", worktree, started_at)
    _write_salvage_transcript(home, worktree, "claude-uuid-2", _no_op_salvage_payload())
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-noop",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-noop",
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=_auto_config()
    )

    assert reverted == []
    reloaded = next(s for s in load_state().sessions if s.id == "salv-noop")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "no_op"
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-noop")
    assert task.status == QueueItemStatus.COMPLETED


def test_revert_stalled_no_salvage_without_sentinel_times_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale transcript, no terminal sentinel → TIMED_OUT + revert (unchanged).

    The transcript's mtime is pinned stale (past the liveness floor) so the
    #1277 wall-clock liveness veto does not fire. Since #1283's widened lookup
    now also globs non-surface_ref-prefixed siblings, an un-pinned (real, fresh)
    mtime would otherwise be seen as live and veto the revert.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-nosent"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-none", worktree, started_at)
    # Transcript exists but carries no AUTO_DEV_RESULT block.
    proj = claude_project_dir(worktree)
    proj.mkdir(parents=True, exist_ok=True)
    tx = proj / "claude-uuid-3.jsonl"
    tx.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "still working on it"}],
                },
            }
        )
        + "\n"
    )
    stale_ts = (started_at + timedelta(seconds=60)).timestamp()
    os.utime(tx, (stale_ts, stale_ts))
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-none",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-none",
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=_auto_config()
    )

    assert "salv-none" in reverted
    reloaded = next(s for s in load_state().sessions if s.id == "salv-none")
    assert reloaded.status == SessionStatus.TIMED_OUT
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-none")
    assert task.status == QueueItemStatus.PENDING


def test_revert_stalled_ignores_stale_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcript older than started_at (reused worktree, #358) → TIMED_OUT."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-stale"
    # started_at in the future relative to the (real-now) transcript mtime, so
    # the freshly-written transcript is "stale" by the started_at guard.
    started_at = datetime(2099, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2099, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-stale", worktree, started_at)
    _write_salvage_transcript(
        home, worktree, "claude-uuid-4", _shipped_salvage_payload()
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-stale",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-stale",
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=_auto_config()
    )

    # Stale transcript ignored → genuine timeout.
    assert "salv-stale" in reverted
    reloaded = next(s for s in load_state().sessions if s.id == "salv-stale")
    assert reloaded.status == SessionStatus.TIMED_OUT


def test_reconcile_includes_stalled_reverts_in_report(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile() surfaces stalled-session reverts in ReconcileReport."""
    worktree = tmp_path / "wt-rec"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    daemon = FakeNativeDaemonClient()
    short_id = daemon.spawn_bg(cwd=tmp_path, prompt="seed")

    sess = _mk_headless_daemon_session(
        "rec-stalled", worktree, started_at, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="rec-stalled",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="rec-stalled",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    # After revert_stalled_headless_sessions fires, session becomes TIMED_OUT,
    # so the outage guard won't trip. Monkeypatch to avoid subprocess.run.
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    with freezegun.freeze_time(now):
        report = reconcile()

    assert "rec-stalled" in report.reverted_ticket_ids
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "rec-stalled")
    assert t.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# resolve_headless_budget tests (GitHub issue #265)
# ---------------------------------------------------------------------------


def test_revert_stalled_uses_per_session_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session with tier='small' (budget=1800) elapsed 2000s → timed out (< 3600)."""
    worktree = tmp_path / "wt-per-sess"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # 2000s elapsed: > 1800 (small tier) but < 3600 (global fallback)
    now = datetime(2026, 1, 1, 0, 33, 20, tzinfo=UTC)
    assert (now - started_at).total_seconds() == 2000

    sess = _mk_headless_daemon_session("per-sess-small", worktree, started_at)
    sess.last_result = {"scope": {"tier": "small"}}
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )

    # Verify resolve_headless_budget returns 1800 for this session
    budget = resolve_headless_budget(None, sess, config)
    assert budget == 1800

    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="per-sess-small",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="per-sess-small",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_stalled_headless_sessions(state, now=now, config=config)

    assert "per-sess-small" in reverted
    assert sess.status == SessionStatus.TIMED_OUT


# ---------------------------------------------------------------------------
# flag_silently_idle_daemon_sessions tests (GitHub issue #129)
# ---------------------------------------------------------------------------


def test_revert_stalled_skips_parked_silently_idle_session(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session parked by flag_silently_idle_daemon_sessions (last_result has
    paused_status=silently_idle) must NOT be reverted to PENDING by the
    wall-clock timeout sweep, even when past the headless budget (#431).
    """
    worktree = tmp_path / "wt-parked"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)  # past HEADLESS_TIMEOUT_SECONDS
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("parked-idle", worktree, started_at)
    # Simulate a session parked by flag_silently_idle_daemon_sessions.
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="parked-idle",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        session_id="parked-idle",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_stalled_headless_sessions(
        state=state, now=now, config=_auto_config()
    )

    assert reverted == [], "Parked (silently_idle) session must NOT be reverted"
    assert sess.status == SessionStatus.ACTIVE, (
        "Parked session status must remain ACTIVE (flag-only park)"
    )
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "parked-idle")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER, (
        "Queue task must remain BLOCKED_ON_USER, not re-dispatched to PENDING"
    )


def test_reap_reason_wall_clock_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """revert_stalled_headless_sessions sets reap_reason=wall_clock_budget."""
    worktree = tmp_path / "wt-wc"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("wc-budget-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "wc-budget-1")
    assert s.reap_reason == ReapReason.WALL_CLOCK_BUDGET


def test_detect_stalled_candidates_under_budget_returns_empty(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session with elapsed < budget → no candidate, bytes unchanged."""
    from cw.reconcile import _detect_stalled_candidates

    worktree = tmp_path / "wt-under"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("under-detect", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert candidates == []
    assert _state_queue_snapshot() == snap


def test_detect_stalled_candidates_revert_task_candidate(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """ACTIVE DAEMON headless session with elapsed > budget → REVERT_TASK candidate."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    worktree = tmp_path / "wt-revert"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("revert-detect-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    task = TicketTask(
        ticket_id="revert-detect-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="revert-detect-1",
    )
    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"revert-detect-1": task},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.REVERT_TASK
    assert c.ticket_id == "revert-detect-1"
    assert c.reap_reason == ReapReason.WALL_CLOCK_BUDGET
    # Purity: no writes
    assert _state_queue_snapshot() == snap


def test_detect_stalled_candidates_skip_parked_candidate(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """session.last_result = {"paused_status": "silently_idle"} → SKIP_PARKED."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    worktree = tmp_path / "wt-skip"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("skip-parked-1", worktree, started_at)
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.SKIP_PARKED
    assert candidates[0].paused_status == _SILENTLY_IDLE_REASON
    assert _state_queue_snapshot() == snap


def test_detect_stalled_candidates_salvage_completion_candidate(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session past budget with terminal sentinel → SALVAGE_COMPLETION."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-salvage"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-detect-1", worktree, started_at)
    payload = _shipped_salvage_payload()
    payload["ticket_id"] = "salv-detect-1"
    _write_salvage_transcript(home, worktree, "csid-uuid", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    assert c.salvage_result is not None
    assert _state_queue_snapshot() == snap


def test_detect_stalled_candidates_skips_user_origin(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """USER-origin session → not in returned list; bytes unchanged."""
    from cw.reconcile import _detect_stalled_candidates

    worktree = tmp_path / "wt-user"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("user-origin-1", worktree, started_at)
    sess.origin = SessionOrigin.USER
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert candidates == []
    assert _state_queue_snapshot() == snap


# --- _detect_idle_candidates ---


def test_act_on_stalled_revert_task_updates_state_and_queue(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERT_TASK candidate → TIMED_OUT session, PENDING queue, event emitted."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    worktree = tmp_path / "wt-act-revert"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )
    monkeypatch.setattr("cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None)

    sess = _mk_headless_daemon_session("act-revert-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="act-revert-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="act-revert-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="act-revert-1",
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id="act-revert-1",
        elapsed_seconds=3700.0,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
    )

    reverted, _ = _act_on_stalled_candidates(
        state, [candidate], now=now, config=_auto_config()
    )

    assert "act-revert-1" in reverted
    assert sess.status == SessionStatus.TIMED_OUT

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "act-revert-1")
    assert t.status == QueueItemStatus.PENDING

    events = read_events(
        consumer="test-act-revert-1",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    # Must NOT emit SESSION_COMPLETED for REVERT_TASK path
    completed_events = read_events(
        consumer="test-act-revert-1-completed",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(completed_events) == 0


def test_act_on_stalled_salvage_completion(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SALVAGE_COMPLETION candidate → session COMPLETED, queue COMPLETED."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )

    worktree = tmp_path / "wt-act-salv"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("act-salv-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="act-salv-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="act-salv-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    result = AutoDevResult.model_validate(_shipped_salvage_payload())
    candidate = ReapCandidate(
        session_id="act-salv-1",
        proposed_action=ProposedAction.SALVAGE_COMPLETION,
        ticket_id="act-salv-1",
        salvage_result=result,
        salvage_csid="csid-act-salv",
    )

    _act_on_stalled_candidates(state, [candidate], now=now)

    assert sess.status == SessionStatus.COMPLETED
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "act-salv-1")
    assert t.status == QueueItemStatus.COMPLETED


def test_act_on_stalled_skip_parked_emits_event_no_queue_change(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """SKIP_PARKED candidate → SESSION_SALVAGE_SKIPPED emitted; queue unchanged."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    worktree = tmp_path / "wt-skip-act"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("act-skip-1", worktree, started_at)
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="act-skip-1",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        session_id="act-skip-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="act-skip-1",
        proposed_action=ProposedAction.SKIP_PARKED,
        ticket_id="act-skip-1",
        paused_status=_SILENTLY_IDLE_REASON,
    )

    _act_on_stalled_candidates(state, [candidate], now=now)

    # Queue must be unchanged
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "act-skip-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER

    events = read_events(
        consumer="test-act-skip-1",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 1


def test_act_on_stalled_salvage_completion_emits_session_completed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SALVAGE_COMPLETION → SESSION_COMPLETED event emitted with salvaged=True."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )

    worktree = tmp_path / "wt-salv-evt"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("act-salv-evt-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    payload = _shipped_salvage_payload()
    payload["ticket_id"] = "act-salv-evt-1"
    result = AutoDevResult.model_validate(payload)
    candidate = ReapCandidate(
        session_id="act-salv-evt-1",
        proposed_action=ProposedAction.SALVAGE_COMPLETION,
        ticket_id="act-salv-evt-1",
        salvage_result=result,
        salvage_csid="csid-salv-evt",
    )

    _act_on_stalled_candidates(state, [candidate], now=now)

    events = read_events(
        consumer="test-act-salv-evt-1",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert events[0].payload["salvaged"] is True
    assert events[0].payload["crashed"] is False


def test_detect_stalled_needs_salvage_reason_skip_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """session.last_result with _NEEDS_SALVAGE_REASON → SKIP_PARKED candidate."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    worktree = tmp_path / "wt-needs-salvage"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("needs-salvage-1", worktree, started_at)
    sess.last_result = {"paused_status": _NEEDS_SALVAGE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.SKIP_PARKED
    assert candidates[0].paused_status == _NEEDS_SALVAGE_REASON
    assert _state_queue_snapshot() == snap


def test_stalled_cap_park_candidate_stamps_disposition(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """_detect_stalled_candidates's cap-exceeded PARK_BLOCKED_ON_USER carries
    paused_status (#976)."""
    from cw.reconcile import (
        _STALLED_CAP_PARKED_REASON,
        DEFAULT_STALLED_RETRY_CAP,
        ProposedAction,
        _detect_stalled_candidates,
    )

    worktree = tmp_path / "wt-cap-disp"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("cap-disp-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cap-disp-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cap-disp-1",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(headless_timeout_by_stage={}),
        task_by_ticket={"cap-disp-1": task},
    )

    park = next(
        c
        for c in candidates
        if c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    )
    assert park.paused_status == _STALLED_CAP_PARKED_REASON
