"""Shared builders for the merge-prerequisite tests (#3791).

Plain functions, no fixtures, so this module is not registered in
``tests/conftest.py`` (precedent: ``scope_grant_builders``). One home for the
claim-gate helpers and the repo-slug patch, so the CLI, core and claim tests
do not grow private copies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorConfig,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.models.merge_prerequisite import MergePrerequisite
from cw.models.orchestrator_config.lane import LaneConfig
from cw.models.pr_ref import PrRef
from tests._clients_yaml import write_clients_yaml

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cw.native_daemon import FakeNativeDaemonClient

MP_CLIENT = "mp-client"
MP_TICKET = "MP-1"
MP_BRANCH = "dev/mp-1"
MP_LANE = "alpha"
MP_SLUG = "foo/bar"
DECLARED_AT = datetime(2026, 11, 20, tzinfo=UTC)
SEEDED_ATTEMPTS = 3
SEEDED_UNPRODUCTIVE = 2
SEEDED_REGRESS = 1


def patch_repo_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slug: str | None = MP_SLUG
) -> None:
    """Run outside any dispatch worktree; the client's origin resolves to *slug*."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "cw.cli.dev_queue.hold._resolve_repo_slug", lambda _git_dir: slug
    )


def claim_client(tmp_path: Path, *, name: str = MP_CLIENT) -> ClientConfig:
    """A minimal client with one lane, ``alpha``."""
    return ClientConfig(
        name=name,
        workspace_path=tmp_path,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
        lanes=[LaneConfig(name=MP_LANE)],
    )


def prerequisite_entry(
    number: int, *, repo: str = MP_SLUG, satisfied: bool = False
) -> MergePrerequisite:
    """A same-repo prerequisite on PR *number*, verified against *repo*."""
    return MergePrerequisite(
        pr=PrRef(number=number),
        repo=repo,
        actor="operator",
        declared_at=DECLARED_AT,
        satisfied_at=DECLARED_AT if satisfied else None,
    )


def pending_row(**overrides: Any) -> TicketTask:
    """A PENDING REVIEW row with seeded counters, *overrides* applied."""
    fields: dict[str, Any] = {
        "ticket_id": MP_TICKET,
        "client": MP_CLIENT,
        "lane": MP_LANE,
        "stage": Stage.REVIEW,
        "status": QueueItemStatus.PENDING,
        "branch": MP_BRANCH,
        "attempts": SEEDED_ATTEMPTS,
        "unproductive_attempts": SEEDED_UNPRODUCTIVE,
        "regress_attempts": SEEDED_REGRESS,
    }
    fields.update(overrides)
    return TicketTask.model_validate(fields)


def seed_rows(*rows: TicketTask) -> None:
    """Persist *rows* as the whole dev queue."""
    save_dev_queue(DevQueueStore(tasks=list(rows)))


def stored_row(ticket_id: str = MP_TICKET) -> TicketTask:
    """The persisted row for *ticket_id*."""
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)


def _claim_config(client_name: str) -> OrchestratorConfig:
    return OrchestratorConfig(
        tick_interval_seconds=30, per_client_max_parallel={client_name: 4}
    )


def claim_next(
    client: ClientConfig, lane: str = MP_LANE, **kwargs: Any
) -> TicketTask | None:
    """Run one ``_claim_next_pending`` and return only the claimed row."""
    from cw.dispatch.claim.screening import _claim_next_pending

    claimed, _ = _claim_next_pending(
        client.name,
        lane=lane,
        client=client,
        config=_claim_config(client.name),
        review_approval_holds=None,
        **kwargs,
    )
    return claimed


def write_gate_client(tmp_path: Path, workspace: Path) -> None:
    """Write ``clients.yaml`` for the claim client rooted at *workspace*."""
    client = claim_client(tmp_path)
    write_clients_yaml(client.model_copy(update={"workspace_path": workspace}))


def gate_tick(daemon: FakeNativeDaemonClient) -> Any:
    """One real ``dispatch_tick`` against *daemon*."""
    from cw.dispatch import dispatch_tick

    return dispatch_tick(config=_claim_config(MP_CLIENT), native_daemon=daemon)
