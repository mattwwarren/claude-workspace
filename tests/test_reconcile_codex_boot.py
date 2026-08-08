"""Tests for cw.reconcile.codex_boot — boot-time orphaned-codex-session pass (#1727).

Once ``CodexExecutor.spawn()`` hands its review to a background thread, a
crash/SIGKILL can leave an ACTIVE codex session behind with no thread left to
join. This pass, run once before the first dispatch tick, flags exactly those
for operator attention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.config import save_state
from cw.dev_queue import add_ticket, load_dev_queue
from cw.events import read_events
from cw.models import (
    CwState,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile.codex_boot import (
    CODEX_ORPHANED_AT_BOOT_DISPOSITION,
    reap_orphaned_codex_sessions_at_boot,
)
from tests._reconcile_helpers import _mk_headless_daemon_session

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _write_clients_yaml(tmp_config_dir: Path, workspace: Path, backend: str) -> None:
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n"
        "  client-a:\n"
        f"    workspace_path: {workspace}\n"
        "    default_branch: main\n"
        "    pipeline:\n"
        "      executors:\n"
        "        review:\n"
        f"          backend: {backend}\n"
    )


def _seed(
    tmp_config_dir: Path,
    tmp_path: Path,
    *,
    backend: str = "codex",
    ticket_id: str = "T-orphan",
    session: Session | None = None,
) -> None:
    """Write clients.yaml, one ACTIVE codex session, and its RUNNING task."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_clients_yaml(tmp_config_dir, workspace, backend)
    sess = session or _mk_headless_daemon_session(
        ticket_id, tmp_path / "wt", _STARTED_AT
    )
    save_state(CwState(sessions=[sess]))
    add_ticket(
        TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id=sess.id,
        )
    )


def _attention_events(consumer: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
    ]


def test_orphaned_codex_session_is_flagged(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """The one case this pass exists for: park the task, emit the signal."""
    _seed(tmp_config_dir, tmp_path)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    assert task.session_id is None

    payloads = _attention_events("test-codex-boot-flagged")
    assert len(payloads) == 1
    assert payloads[0]["paused_status"] == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    assert payloads[0]["ticket_id"] == "T-orphan"
    assert payloads[0]["client"] == "client-a"
    # Breadcrumbs must point the operator at the real risk: a partial commit.
    assert "worktree" in str(payloads[0]["breadcrumbs"])


def test_non_codex_backend_session_is_left_alone(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A claude-native REVIEW session at boot is not this pass's business."""
    _seed(tmp_config_dir, tmp_path, backend="claude-native")

    assert reap_orphaned_codex_sessions_at_boot() == 0

    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING
    assert _attention_events("test-codex-boot-non-codex") == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", SessionStatus.COMPLETED),
        ("origin", SessionOrigin.USER),
    ],
)
def test_ineligible_session_shape_is_skipped(
    tmp_config_dir: Path, tmp_path: Path, field: str, value: object
) -> None:
    """Only live DAEMON sessions are eligible — mirrors the stalled sweep's gate."""
    sess = _mk_headless_daemon_session("T-orphan", tmp_path / "wt", _STARTED_AT)
    setattr(sess, field, value)
    _seed(tmp_config_dir, tmp_path, session=sess)

    assert reap_orphaned_codex_sessions_at_boot() == 0
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


def test_non_headless_session_is_skipped(tmp_config_dir: Path, tmp_path: Path) -> None:
    """An interactive session's worktree is not a headless orphan."""
    sess = _mk_headless_daemon_session("T-orphan", tmp_path / "wt", _STARTED_AT)
    _seed(tmp_config_dir, tmp_path, session=sess)
    (tmp_path / "wt" / ".claude" / "cw-context.json").write_text('{"headless": false}')

    assert reap_orphaned_codex_sessions_at_boot() == 0
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


def test_session_without_a_matching_task_is_skipped_without_raising(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """No dev-queue row to park → skip quietly; never raise on a boot path."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_clients_yaml(tmp_config_dir, workspace, "codex")
    save_state(
        CwState(
            sessions=[
                _mk_headless_daemon_session("T-nope", tmp_path / "wt", _STARTED_AT)
            ]
        )
    )

    assert reap_orphaned_codex_sessions_at_boot() == 0


def test_session_with_unparseable_name_is_skipped(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A session name carrying no auto-dev ticket id yields no ticket to park."""
    sess = _mk_headless_daemon_session("T-orphan", tmp_path / "wt", _STARTED_AT)
    sess.name = "client-a/interactive-impl"
    _seed(tmp_config_dir, tmp_path, session=sess)

    assert reap_orphaned_codex_sessions_at_boot() == 0
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


def test_unknown_client_is_skipped(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A session whose client is no longer declared cannot resolve a backend."""
    _seed(tmp_config_dir, tmp_path)
    config_dir = tmp_config_dir / ".config" / "cw"
    (config_dir / "clients.yaml").write_text(
        "clients:\n  other:\n    workspace_path: /tmp\n"
    )

    assert reap_orphaned_codex_sessions_at_boot() == 0
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING
