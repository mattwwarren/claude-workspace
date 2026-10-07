"""Pre-spawn stale ``.cw/context.json`` invalidation tests (#1046).

Covers ``cw.dispatch.gating``'s context-json family. Split out of
``tests/test_dispatch.py`` (#2503).
"""

from __future__ import annotations

from pathlib import Path

from cw.dev_queue import add_ticket
from cw.dispatch import dispatch_tick
from cw.models import (
    ClientConfig,
    OrchestratorConfig,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml

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
