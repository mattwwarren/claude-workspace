"""Pre-spawn stale ``.cw/context.json`` invalidation tests (#1046).

Covers ``cw.dispatch.gating.context_json``. Split out of
``tests/test_dispatch.py`` (#2503).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from cw.dev_queue import add_ticket, load_dev_queue
from cw.dev_queue.requeue import requeue_ticket
from cw.dev_queue.stage_entry import enter_stage
from cw.dispatch import dispatch_tick
from cw.models import (
    ClientConfig,
    OrchestratorConfig,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.ticket_snapshot import write_snapshot
from tests._clients_yaml import write_clients_yaml
from tests._shared.ticket_context import make_snapshot
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# TestSpawnInvalidatesStaleContextJson (#1046)
# ---------------------------------------------------------------------------


class TestSpawnInvalidatesStaleContextJson:
    """Pre-spawn invalidation of a stale ``.cw/context.json`` (#1046).

    A requeued/re-spawned task (``attempts > 1``) must not let a worker
    silently replan against a prior session's materialized context — see
    #1030 for the incident this guards against. LocalExecutor is excluded:
    ``local_runner.build_task_message`` reads ``.cw/context.json`` and
    degrades silently to an empty ``## Ticket:`` header if it disappears
    (the #952 regression class).
    """

    def test_deletes_stale_context_json_for_non_local_executor(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """attempts > 1 + non-local (default) executor -> stale
        .cw/context.json is removed before spawn."""
        from cw.worktree import create_worktree

        write_clients_yaml(sample_client_config)

        branch = f"{sample_client_config.feature_branch_prefix}/GEN-STALE"
        worktree_path = create_worktree(
            sample_client_config, branch, allow_dirty_reuse=True
        )
        context_file = worktree_path / ".cw" / "context.json"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text('{"sentinel": "stale"}')

        add_ticket(TicketTask(ticket_id="GEN-STALE", client="test-client", attempts=1))

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 1
        assert not context_file.exists()

    def test_preserves_context_json_for_local_executor(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """attempts > 1 + LOCAL_BACKEND executor -> .cw/context.json
        survives (local_runner.build_task_message reads it; deleting it
        would recreate the #952 empty-header regression)."""
        from cw.worktree import create_worktree

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        clients_file = config_dir / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  test-client:\n"
            f"    workspace_path: {sample_client_config.workspace_path}\n"
            f"    default_branch: {sample_client_config.default_branch}\n"
            f"    worktree_base: {sample_client_config.worktree_base}\n"
            "    pipeline:\n"
            "      executors:\n"
            "        plan:\n"
            "          backend: local\n"
        )

        branch = f"{sample_client_config.feature_branch_prefix}/GEN-STALE-LOCAL"
        worktree_path = create_worktree(
            sample_client_config, branch, allow_dirty_reuse=True
        )
        context_file = worktree_path / ".cw" / "context.json"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text('{"sentinel": "preserved"}')

        add_ticket(
            TicketTask(ticket_id="GEN-STALE-LOCAL", client="test-client", attempts=1)
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        assert context_file.exists()
        assert context_file.read_text() == '{"sentinel": "preserved"}'


class TestSpawnRendersContextJsonFromSnapshot:
    """Dispatch spawns re-materialize the snapshot-backed context."""

    @staticmethod
    def _snapshot_digest(ticket_id: str, client: str) -> str:
        return write_snapshot(make_snapshot(ticket_id=ticket_id, client=client))

    def test_requeue_respawn_rerenders_from_store(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.worktree import create_worktree

        write_clients_yaml(sample_client_config)
        ticket_id = "GEN-SNAPSHOT-REQUEUE"
        digest = self._snapshot_digest(ticket_id, sample_client_config.name)
        branch = f"{sample_client_config.feature_branch_prefix}/{ticket_id}"
        worktree = create_worktree(sample_client_config, branch, allow_dirty_reuse=True)
        context_file = worktree / ".cw" / "context.json"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text('{"sentinel": "stale"}')
        add_ticket(
            TicketTask(
                ticket_id=ticket_id,
                client=sample_client_config.name,
                status=QueueItemStatus.BLOCKED_ON_USER,
                session_id="sess-snapshot",
                attempts=1,
                snapshot_digest=digest,
            )
        )
        requeue_ticket(
            ticket_id,
            sample_client_config.name,
            native_daemon=FakeNativeDaemonClient(),
        )
        gh = FakeGhCli().install(monkeypatch)

        result = dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert result.spawned == 1
        assert not any(call[1:3] == ["issue", "view"] for call in gh.calls)
        assert context_file.exists()
        assert '"sentinel": "stale"' not in context_file.read_text()
        assert '"ticket_id": "GEN-SNAPSHOT-REQUEUE"' in context_file.read_text()

    def test_stage_advance_rerenders_from_store(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        from cw.worktree import create_worktree

        write_clients_yaml(sample_client_config)
        ticket_id = "GEN-SNAPSHOT-STAGE"
        digest = self._snapshot_digest(ticket_id, sample_client_config.name)
        task = TicketTask(
            ticket_id=ticket_id,
            client=sample_client_config.name,
            attempts=1,
            snapshot_digest=digest,
        )
        enter_stage(task, Stage.IMPL)
        add_ticket(task)
        branch = f"{sample_client_config.feature_branch_prefix}/{ticket_id}"
        worktree = create_worktree(sample_client_config, branch, allow_dirty_reuse=True)

        result = dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert result.spawned == 1
        assert (
            next(
                row for row in load_dev_queue().tasks if row.ticket_id == ticket_id
            ).attempts
            > 1
        )
        assert (worktree / ".cw" / "context.json").exists()

    def test_local_render_failure_still_spawns_without_reader_file(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        from cw.worktree import create_worktree

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  test-client:\n"
            f"    workspace_path: {sample_client_config.workspace_path}\n"
            f"    default_branch: {sample_client_config.default_branch}\n"
            f"    worktree_base: {sample_client_config.worktree_base}\n"
            "    pipeline:\n"
            "      executors:\n"
            "        plan:\n"
            "          backend: local\n"
        )
        ticket_id = "GEN-SNAPSHOT-LOCAL"
        branch = f"{sample_client_config.feature_branch_prefix}/{ticket_id}"
        worktree = create_worktree(sample_client_config, branch, allow_dirty_reuse=True)
        context_file = worktree / ".cw" / "context.json"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text('{"sentinel": "stale"}')
        add_ticket(
            TicketTask(
                ticket_id=ticket_id,
                client=sample_client_config.name,
                attempts=1,
                snapshot_digest="c" * 64,
            )
        )

        result = dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert result.spawned == 1
        assert not context_file.exists()

    def test_unremovable_reader_file_does_not_abort_spawn(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.dispatch.gating import context_json
        from cw.worktree import create_worktree

        write_clients_yaml(sample_client_config)
        ticket_id = "GEN-SNAPSHOT-CLEANUP"
        branch = f"{sample_client_config.feature_branch_prefix}/{ticket_id}"
        worktree = create_worktree(sample_client_config, branch, allow_dirty_reuse=True)
        context_file = worktree / ".cw" / "context.json"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text('{"sentinel": "stale"}')
        add_ticket(
            TicketTask(
                ticket_id=ticket_id,
                client=sample_client_config.name,
                attempts=1,
                snapshot_digest="c" * 64,
            )
        )
        monkeypatch.setattr(context_json, "_remove_stale_context", lambda *_a: False)
        monkeypatch.setattr(
            context_json, "_invalidate_stale_context_json", lambda *_a: True
        )

        result = dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert result.spawned == 1
        assert context_file.exists()
