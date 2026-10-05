"""Shared test helpers for the codex crash-recovery test suites.

Fixtures, event readers, and patch helpers used by two or more of
``test_reconcile_codex_boot.py``, ``test_reconcile_codex_harvest.py``,
``test_reconcile_codex_reparks.py`` and ``test_codex_legacy_recovery.py``.
This module has no ``test_`` prefix, so pytest does not collect it (same
convention as ``tests/_codex_review_helpers.py``); it is imported explicitly
by the test modules that use each helper.

The patch helpers bind ``cw.reconcile.codex_boot`` names
(``load_effective_config``, ``_codex_processes_in``): every recovery path that
scans for a live codex writer goes through that module's scan.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.config import load_state, save_state
from cw.dev_queue import add_ticket
from cw.events import read_events
from cw.models import (
    CompletionReason,
    CwState,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    Session,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import codex_boot
from tests._clients_yaml import review_backend_clients, write_clients_yaml
from tests._reconcile_helpers import _mk_headless_daemon_session
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import ModuleType

    import pytest

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _attention_events(consumer: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
    ]


def _requeued_events(consumer: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.TICKET_REQUEUED],
        )
    ]


def _use_config(
    monkeypatch: pytest.MonkeyPatch | None = None, **fields: object
) -> OrchestratorConfig:
    """Pin the orchestrator config the boot pass resolves its gates against."""
    config = OrchestratorConfig.model_validate(fields)
    if monkeypatch is not None:
        monkeypatch.setattr(codex_boot, "load_effective_config", lambda: config)
    return config


def _use_auto_reap_policy(
    monkeypatch: pytest.MonkeyPatch | None = None, **fields: object
) -> OrchestratorConfig:
    """Authorize the requeue branch (gate 0) so a later gate is what decides."""
    return _use_config(monkeypatch, reap_policy=ReapPolicy.AUTO, **fields)


def _no_codex_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: [])


def _live_writer(monkeypatch: pytest.MonkeyPatch, *pids: int) -> None:
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: list(pids))


class _FakeCodex:
    """A process-table entry for a codex writer that records any signal.

    Stands in for what ``psutil.process_iter`` yields, so the real cwd scan
    runs against it. Every signalling method records instead of acting: the
    boot pass must never call one.
    """

    def __init__(self, worktree: Path, pid: int = 4242) -> None:
        self.pid = pid
        self.info: dict[str, object] = {"name": "codex", "cwd": str(worktree)}
        self.signals: list[str] = []

    def terminate(self) -> None:
        self.signals.append("SIGTERM")

    def kill(self) -> None:
        self.signals.append("SIGKILL")

    def send_signal(self, sig: int) -> None:
        self.signals.append(str(sig))


def _forbid_os_kill(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record any ``os.kill`` instead of sending it; the pass must send none."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def _completed_events(consumer: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
    ]


def _failing_record_event(
    monkeypatch: pytest.MonkeyPatch,
    target: ModuleType,
    failing_type: OrchestratorEventType,
) -> Callable[..., Any]:
    """Make *target*.record_event raise OSError for *failing_type* only.

    Returns the real ``record_event`` so a test can restore it for a retry.
    """
    real: Callable[..., Any] = target.record_event

    def _record(event_type: OrchestratorEventType, *args: Any, **kwargs: Any) -> Any:
        if event_type is failing_type:
            msg = "disk full"
            raise OSError(msg)
        return real(event_type, *args, **kwargs)

    monkeypatch.setattr(target, "record_event", _record)
    return real


def _assert_session_closed() -> None:
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_reason is CompletionReason.CRASHED
    assert session.completed_at is not None


def _assert_session_left_active() -> Session:
    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.completed_at is None
    assert session.completed_reason is None
    return session


def _seed_clean_codex_orphan(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    *,
    ticket_id: str = "T-orphan",
) -> tuple[Path, str]:
    """A real git repo, .claude/cw-context.json + review-verdict.md committed
    vs. left dirty per the 'clean except verdict' contract, stage_base_ref
    stamped to HEAD as of the commit *before* review-verdict.md is added.

    Searched for overlapping siblings: none found — closest is
    test_dispatch_branch_freshness.py's _seed_repo, a different domain
    (branch-fetch freshness fixture, not a review-orphan session/task pair).
    """
    repo = make_git_repo("wt")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_clients_yaml(*review_backend_clients(workspace, "codex"))
    sess = _mk_headless_daemon_session(ticket_id, repo, _STARTED_AT)
    # Why: intentionally overwrites the session_id key _mk_headless_daemon_session
    # just wrote, to establish a clean committed baseline — a real review-orphan
    # repo's last commit wouldn't carry a stale session id.
    commit_tracked_file(repo, ".claude/cw-context.json", '{"headless": true}')
    head_sha = git_in(repo, "rev-parse", "HEAD")
    save_state(CwState(sessions=[sess]))
    add_ticket(
        TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id=sess.id,
            stage_base_ref=head_sha,
        )
    )
    (repo / ".claude" / "review-verdict.md").write_text("verdict text\n")
    return repo, head_sha


def _task_without_base_ref() -> TicketTask:
    return TicketTask(
        ticket_id="T-orphan",
        client="client-a",
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
        session_id="T-orphan",
    )
