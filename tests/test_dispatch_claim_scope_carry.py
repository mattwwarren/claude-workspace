"""Dispatch-level regression for the scope-grant carry (#3772).

The operator's ``plan_scope_drift`` approval is transported to a worker through
``scope_drift_approved_*`` on the row, and the spawn-success stamp clears that
transport. The durable ledger (``scope_grants``) must therefore re-populate it
at every IMPL dispatch, including the REVIEW-to-IMPL repair redispatch that used
to lose the grant. These tests drive the real ``dispatch_tick`` claim path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import add_ticket, load_dev_queue, save_dev_queue
from cw.dispatch import dispatch_tick
from cw.models import QueueItemStatus, SessionStatus, Stage, TicketTask
from cw.models.tasks.keys import (
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.scope_grants import review_policy_fingerprint
from cw.worktree import worktree_path_for
from tests._clients_yaml import write_clients_yaml
from tests._shared.git_repos import git_in
from tests._shared.scope_grant_builders import GRANT_SLUG, make_grant

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig
    from cw.models.scope_grant import ScopeGrant

_TICKET = "GEN-3772"
_CLIENT = "test-client"
_PATHS = ["src/a.py", "src/b.py"]


@pytest.fixture
def slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the worktree's origin slug to the grants' repository."""
    monkeypatch.setattr(
        "cw.dispatch.claim.spawn._resolve_repo_slug", lambda _path: GRANT_SLUG
    )


def _row(
    client: ClientConfig, workspace: Path, *, stage: Stage, created_at: datetime
) -> TicketTask:
    """A PENDING row holding one grant at the workspace HEAD (a real commit)."""
    head = git_in(workspace, "rev-parse", "HEAD")
    grant: ScopeGrant = make_grant(
        client=_CLIENT,
        ticket_id=_TICKET,
        row_created_at=created_at,
        head=head,
        paths=_PATHS,
        policy=review_policy_fingerprint(client.review),
    )
    return TicketTask(
        ticket_id=_TICKET,
        client=_CLIENT,
        stage=stage,
        created_at=created_at,
        scope_grants=[grant],
    )


def _context(client: ClientConfig) -> dict[str, object]:
    branch = f"{client.feature_branch_prefix}/{_TICKET}"
    path = worktree_path_for(client, branch) / ".claude" / "cw-context.json"
    context: dict[str, object] = json.loads(path.read_text())
    return context


def _queue_metadata(client: ClientConfig) -> dict[str, object]:
    metadata = _context(client)["queue_metadata"]
    assert isinstance(metadata, dict)
    return metadata


def _mark_sessions_terminal() -> None:
    """The first worker finished: its worktree is free to be reused."""
    state = load_state()
    for session in state.sessions:
        session.status = SessionStatus.COMPLETED
    save_state(state)


class TestScopeGrantCarryAtSpawn:
    def test_impl_spawn_writes_the_carried_union_into_queue_metadata(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        workspace_dir: Path,
        slug: None,
    ) -> None:
        write_clients_yaml(sample_client_config)
        created = datetime(2026, 11, 2, tzinfo=UTC)
        row = _row(
            sample_client_config, workspace_dir, stage=Stage.IMPL, created_at=created
        )
        add_ticket(row)

        spawned = dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert spawned.spawned == 1
        metadata = _queue_metadata(sample_client_config)
        assert metadata[SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY] == _PATHS
        assert metadata[SCOPE_DRIFT_APPROVED_HEAD_KEY] == row.scope_grants[0].head
        stored = load_dev_queue().tasks[0]
        assert stored.status == QueueItemStatus.RUNNING
        # The spawn-success stamp consumed the transport; the ledger survives.
        assert stored.scope_drift_approved_extra_files is None
        assert stored.scope_drift_approved_head is None
        assert stored.scope_grants == row.scope_grants

    def test_the_repair_redispatch_carries_the_grant_again(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        workspace_dir: Path,
        slug: None,
    ) -> None:
        """THE defect: the second IMPL dispatch used to find no grant at all."""
        write_clients_yaml(sample_client_config)
        created = datetime(2026, 11, 2, tzinfo=UTC)
        row = _row(
            sample_client_config, workspace_dir, stage=Stage.IMPL, created_at=created
        )
        add_ticket(row)
        assert (
            dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient()).spawned
            == 1
        )
        assert load_dev_queue().tasks[0].scope_drift_approved_head is None
        # REVIEW sent the ticket back: same row, same ledger, new IMPL dispatch.
        _mark_sessions_terminal()
        requeued = load_dev_queue()
        requeued.tasks[0].status = QueueItemStatus.PENDING
        requeued.tasks[0].session_id = None
        save_dev_queue(requeued)

        spawned = dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert spawned.spawned == 1
        metadata = _queue_metadata(sample_client_config)
        assert metadata[SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY] == _PATHS
        assert metadata[SCOPE_DRIFT_APPROVED_HEAD_KEY] == row.scope_grants[0].head

    def test_a_non_impl_spawn_carries_nothing(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        workspace_dir: Path,
        slug: None,
    ) -> None:
        write_clients_yaml(sample_client_config)
        created = datetime(2026, 11, 2, tzinfo=UTC)
        add_ticket(
            _row(
                sample_client_config,
                workspace_dir,
                stage=Stage.REVIEW,
                created_at=created,
            )
        )

        spawned = dispatch_tick(simple_config, native_daemon=FakeNativeDaemonClient())

        assert spawned.spawned == 1
        metadata = _queue_metadata(sample_client_config)
        assert metadata[SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY] is None
        assert metadata[SCOPE_DRIFT_APPROVED_HEAD_KEY] is None
