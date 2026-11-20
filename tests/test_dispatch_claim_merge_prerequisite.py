"""Claim-time merge-prerequisite gate (#3791).

A PENDING row with an unsatisfied same-repo prerequisite is parked as a
``dependency_hold`` / ``dependency_unmerged`` on that PR before any claim. The
park is uncharged (attempts, unproductive and regress counters untouched), the
stage is unchanged, no lane slot is held and nothing is spawned. The existing
stale-gate release returns the row to PENDING on a verified merge only,
stamping ``satisfied_at`` so the next claim does not re-park it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result import DEPENDENCY_UNMERGED_BLOCKER_REASON
from cw.config import save_state
from cw.dev_queue import load_dev_queue, requeue_ticket, save_dev_queue
from cw.dev_queue.hold import check_holdable
from cw.dev_queue.merge_prerequisites import remove_prerequisite
from cw.dispatch.claim.merge_prerequisite import park_unmet_merge_prerequisite
from cw.dispatch.ticket_state_gate import ClosedTicketHit
from cw.exceptions import CwError
from cw.models import (
    CwState,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    Stage,
    WatchedPr,
)
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION
from cw.models.pr_ref import PrRef
from cw.models.tasks.store import occupies_lane_slot
from cw.reconcile.tasks import release_stale_gated_tasks
from tests._shared.git_repos import git_in
from tests._shared.merge_prerequisite_builders import (
    MP_BRANCH,
    MP_CLIENT,
    MP_SLUG,
    MP_TICKET,
    SEEDED_ATTEMPTS,
    SEEDED_REGRESS,
    SEEDED_UNPRODUCTIVE,
    claim_client,
    claim_next,
    gate_tick,
    pending_row,
    prerequisite_entry,
    seed_rows,
    stored_row,
    write_gate_client,
)
from tests._ticket_view_helpers import events_of

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask
    from cw.native_daemon import FakeNativeDaemonClient

_ADOPTED_URL = "https://github.com/foo/bar/pull/77"


def _assert_parked_uncharged(row: TicketTask, number: int, stage: Stage) -> None:
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.disposition == DEPENDENCY_HOLD_DISPOSITION
    assert row.blocked_reason == DEPENDENCY_UNMERGED_BLOCKER_REASON
    assert row.blocked_on_pr == PrRef(number=number)
    assert row.session_id is None
    assert row.stage == stage
    assert row.attempts == SEEDED_ATTEMPTS
    assert row.unproductive_attempts == SEEDED_UNPRODUCTIVE
    assert row.regress_attempts == SEEDED_REGRESS


def _watch(state: str) -> WatchedPr:
    return WatchedPr(
        pr_url="https://github.com/foo/bar/pull/9",
        repo=MP_SLUG,
        pr_number=9,
        client=MP_CLIENT,
        source="dependency_hold",
        cross_repo=False,
        pr_state=PrState(state=state),
    )


def _set_watch(state: str) -> None:
    store = load_dev_queue()
    store.watched_prs = [_watch(state)]
    save_dev_queue(store)


def _closed_unmerged_events() -> list[object]:
    return [
        e
        for e in events_of(OrchestratorEventType.SESSION_REAP_PROPOSED)
        if e.payload.get("proposed_action") == "dependency_hold_pr_closed_unmerged"
    ]


@pytest.mark.parametrize(
    "stage", [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
)
def test_claim_parks_an_unmet_prerequisite_uncharged_at_every_stage(
    tmp_path: Path, stage: Stage
) -> None:
    seed_rows(pending_row(stage=stage, merge_prerequisites=[prerequisite_entry(9)]))

    assert claim_next(claim_client(tmp_path)) is None

    _assert_parked_uncharged(stored_row(), 9, stage)
    [transition] = events_of(OrchestratorEventType.TASK_TRANSITION)
    assert transition.payload["unproductive_charge"] is None


def test_dispatch_tick_never_spawns_for_a_row_with_an_unmerged_prerequisite(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    mock_native_daemon: FakeNativeDaemonClient,
) -> None:
    repo, _origin = make_git_repo_with_origin("mp-park", branch=MP_BRANCH)
    git_in(repo, "checkout", "main")
    write_gate_client(tmp_path, repo)
    seed_rows(pending_row(merge_prerequisites=[prerequisite_entry(9)]))

    result = gate_tick(mock_native_daemon)

    assert result.spawned == 0
    assert mock_native_daemon.spawn_calls == []
    _assert_parked_uncharged(stored_row(), 9, Stage.REVIEW)


def test_park_keeps_the_adopted_pr_and_frees_the_lane_slot(tmp_path: Path) -> None:
    seed_rows(
        pending_row(
            adopted_pr_url=_ADOPTED_URL,
            pr_url=_ADOPTED_URL,
            merge_prerequisites=[prerequisite_entry(9)],
        )
    )

    claim_next(claim_client(tmp_path))

    parked = stored_row()
    assert parked.adopted_pr_url == _ADOPTED_URL
    assert parked.pr_url == _ADOPTED_URL
    assert occupies_lane_slot(parked) is False


def test_park_keeps_a_non_adopted_pr_url(tmp_path: Path) -> None:
    """BLOCKED_ON_USER assigns pr_url unconditionally; the park carries it."""
    own = "https://github.com/foo/bar/pull/55"
    seed_rows(pending_row(pr_url=own, merge_prerequisites=[prerequisite_entry(9)]))

    claim_next(claim_client(tmp_path))

    parked = stored_row()
    assert parked.status == QueueItemStatus.BLOCKED_ON_USER
    assert parked.pr_url == own


def test_park_records_one_ticket_held_event(tmp_path: Path) -> None:
    seed_rows(
        pending_row(
            session_id="stale-sess", merge_prerequisites=[prerequisite_entry(9)]
        )
    )

    claim_next(claim_client(tmp_path))

    [held] = events_of(OrchestratorEventType.TICKET_HELD)
    assert held.correlation_id == MP_TICKET
    assert held.payload == {
        "ticket_id": MP_TICKET,
        "client": MP_CLIENT,
        "pr_number": 9,
        "outcome": "held",
        "previous_pr": None,
        "previous_disposition": None,
        "previous_blocked_reason": None,
        "converted_from_worker_park": False,
        "cleared_session_id": "stale-sess",
        "pr_repo": None,
        "previous_pr_repo": None,
        "via_ticket": None,
        "repo": MP_SLUG,
    }
    assert stored_row().session_id is None


def test_parks_one_prerequisite_at_a_time_in_declared_order(tmp_path: Path) -> None:
    seed_rows(
        pending_row(merge_prerequisites=[prerequisite_entry(9), prerequisite_entry(10)])
    )
    client = claim_client(tmp_path)

    claim_next(client)
    assert stored_row().blocked_on_pr == PrRef(number=9)

    store = load_dev_queue()
    row = store.tasks[0]
    row.merge_prerequisites[0].satisfied_at = row.created_at
    row.status = QueueItemStatus.PENDING
    row.disposition = None
    row.blocked_on_pr = None
    save_dev_queue(store)

    claim_next(client)
    assert stored_row().blocked_on_pr == PrRef(number=10)


def test_a_cross_repo_entry_is_ignored(tmp_path: Path) -> None:
    entry = prerequisite_entry(9).model_copy(
        update={"pr": PrRef(repo="other/repo", number=9)}
    )
    seed_rows(pending_row(merge_prerequisites=[entry]))

    claimed = claim_next(claim_client(tmp_path))

    assert claimed is not None
    assert claimed.status == QueueItemStatus.RUNNING
    assert stored_row().blocked_on_pr is None


@pytest.mark.parametrize("satisfied", [True, None])
def test_satisfied_or_absent_prerequisites_claim_normally(
    tmp_path: Path, satisfied: bool | None
) -> None:
    entries = [] if satisfied is None else [prerequisite_entry(9, satisfied=True)]
    seed_rows(pending_row(merge_prerequisites=entries))

    claimed = claim_next(claim_client(tmp_path))

    assert claimed is not None
    row = stored_row()
    assert row.status == QueueItemStatus.RUNNING
    assert row.attempts == SEEDED_ATTEMPTS + 1


def test_a_closed_ticket_cancels_rather_than_parks(tmp_path: Path) -> None:
    # The closed-ticket guard applies to PLAN/IMPL rows only.
    row = pending_row(stage=Stage.PLAN, merge_prerequisites=[prerequisite_entry(9)])
    seed_rows(row)

    claim_next(
        claim_client(tmp_path),
        closed_ticket_hits={MP_TICKET: ClosedTicketHit(row.created_at, defer=False)},
    )

    after = stored_row()
    assert after.status == QueueItemStatus.CANCELLED
    assert after.blocked_on_pr is None


def test_park_helper_is_a_no_op_without_prerequisites() -> None:
    seed_rows(pending_row())
    store = load_dev_queue()

    assert not park_unmet_merge_prerequisite(
        store.tasks[0], store, client_name=MP_CLIENT
    )
    assert stored_row().status == QueueItemStatus.PENDING


def test_requeue_while_unmerged_reparks_on_the_next_claim(tmp_path: Path) -> None:
    write_gate_client(tmp_path, tmp_path)
    seed_rows(pending_row(merge_prerequisites=[prerequisite_entry(9)]))
    client = claim_client(tmp_path)
    claim_next(client)

    requeue_ticket(MP_TICKET, MP_CLIENT)
    requeued = stored_row()
    assert requeued.status == QueueItemStatus.PENDING

    assert claim_next(client) is None
    reparked = stored_row()
    assert reparked.status == QueueItemStatus.BLOCKED_ON_USER
    assert reparked.blocked_on_pr == PrRef(number=9)
    # requeue's own counter policy applies; the re-park charges nothing more.
    assert reparked.attempts == requeued.attempts
    assert reparked.unproductive_attempts == requeued.unproductive_attempts
    assert reparked.regress_attempts == requeued.regress_attempts


def test_remove_releases_and_the_next_claim_runs(tmp_path: Path) -> None:
    seed_rows(pending_row(merge_prerequisites=[prerequisite_entry(9)]))
    client = claim_client(tmp_path)
    claim_next(client)

    assert remove_prerequisite(MP_TICKET, MP_CLIENT, PrRef(number=9)).released

    claimed = claim_next(client)
    assert claimed is not None
    assert stored_row().status == QueueItemStatus.RUNNING


def test_hold_still_refuses_a_review_row_with_an_adopted_pr() -> None:
    row = pending_row(adopted_pr_url=_ADOPTED_URL)

    with pytest.raises(CwError, match="row already has PR #77"):
        check_holdable(row, CwState(sessions=[]))


def test_closed_unmerged_prerequisite_stays_held_with_one_event(
    tmp_path: Path,
) -> None:
    seed_rows(pending_row(merge_prerequisites=[prerequisite_entry(9)]))
    save_state(CwState(sessions=[]))
    claim_next(claim_client(tmp_path))
    _set_watch("CLOSED")

    assert release_stale_gated_tasks() == []
    assert release_stale_gated_tasks() == []

    row = stored_row()
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.merge_prerequisites[0].satisfied_at is None
    assert len(_closed_unmerged_events()) == 1


def test_prerequisite_parks_then_releases_only_on_verified_merge_then_claims(
    tmp_path: Path,
) -> None:
    seed_rows(pending_row(merge_prerequisites=[prerequisite_entry(9)]))
    save_state(CwState(sessions=[]))
    client = claim_client(tmp_path)

    assert claim_next(client) is None
    _assert_parked_uncharged(stored_row(), 9, Stage.REVIEW)

    # Fail closed on unknown: no evidence, no release.
    assert release_stale_gated_tasks() == []
    assert stored_row().status == QueueItemStatus.BLOCKED_ON_USER

    _set_watch("OPEN")
    assert release_stale_gated_tasks() == []
    assert stored_row().status == QueueItemStatus.BLOCKED_ON_USER

    _set_watch("MERGED")
    assert release_stale_gated_tasks() == [MP_TICKET]
    released = stored_row()
    assert released.status == QueueItemStatus.PENDING
    assert released.session_id is None
    assert released.merge_prerequisites[0].satisfied_at is not None
    assert released.attempts == SEEDED_ATTEMPTS
    assert [w.status for w in load_dev_queue().watched_prs] == ["dismissed"]

    claimed = claim_next(client)
    assert claimed is not None
    final = stored_row()
    assert final.status == QueueItemStatus.RUNNING
    assert final.attempts == SEEDED_ATTEMPTS + 1
    assert final.unproductive_attempts == SEEDED_UNPRODUCTIVE
    assert final.regress_attempts == SEEDED_REGRESS
