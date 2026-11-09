"""Shared builders for the review sign-off attestation tests (#3040, RFC 0017 II1).

Plain functions and constants, no fixtures (the sibling ``_*_helpers.py``
shape). Composes on the existing shared helpers: ``sentinel_review`` owns the
sentinel envelope, ``make_review_client`` the review client,
``confirmed_ledger`` / ``write_ledger`` the convergence ledger.

Some names below are copies of private constants and fixtures of other test
modules (``GATE_LANES``, ``GATE_NOW``, ``gate_config``, the idle-router
fixtures, ``red_pr_bundle`` and the ``stamp_review_record`` /
``assert_no_review_record`` pair). Hoisting the originals onto these names is
a tracked dedupe debt item (#3040 friction report), not done here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.models import ClientConfig, QueueItemStatus, Stage
from cw.models.branch import resolve_feature_branch
from cw.models.review_attestation import AttestationCounts, ReviewAttestation
from cw.worktree import worktree_path_for
from tests._review_convergence_helpers import (
    confirmed_ledger,
    make_review_client,
    write_ledger,
)
from tests._review_history_helpers import sentinel_review
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

#: The 40-hex reviewed head every seeded review record carries.
SEEDED_REVIEW_SHA = "0123456789abcdef0123456789abcdef01234567"
REVIEW_CLIENT = "test-client"
WRITTEN_AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def attestation_for(head: str, **overrides: object) -> ReviewAttestation:
    """A valid engine-less PASS attestation at *head* (counts 0/0)."""
    fields: dict[str, object] = {
        "ticket_id": "T-1",
        "client": REVIEW_CLIENT,
        "head_sha": head,
        "written_at": WRITTEN_AT,
        "engine": None,
        "reviewer_backend": "claude-native",
        "verdict": "PASS",
        "counts": AttestationCounts(),
    }
    fields.update(overrides)
    return ReviewAttestation.model_validate(fields)


def review_row(worktree: Path | None, **overrides: object) -> TicketTask:
    """A RUNNING REVIEW-stage row bound to *worktree* (``None`` allowed)."""
    fields: dict[str, object] = {
        "stage": Stage.REVIEW,
        "status": QueueItemStatus.RUNNING,
        "client": REVIEW_CLIENT,
        "worktree_path": worktree,
    }
    fields.update(overrides)
    return _make_ticket_task(**fields)


def review_clients(
    worktree: Path, *, convergence: str = "clean"
) -> dict[str, ClientConfig]:
    """A one-entry clients dict built on ``make_review_client``."""
    client = make_review_client(worktree, convergence=convergence)
    return {client.name: client}


def clean_sentinel(
    reviewed_sha: str | None,
    *,
    status: str = "stage_complete",
    must_fix_initial: object = 0,
    should_fix: object = 0,
    agents_run: int = 3,
    recommendation: str = "PROCEED",
) -> dict[str, object]:
    """A clean REVIEW sentinel: success status, PROCEED health, a reviewed sha."""
    review: dict[str, object] = {
        "must_fix_initial": must_fix_initial,
        "should_fix": should_fix,
        "fix_cycles_used": 0,
        "deferred": 0,
        "agents_run": agents_run,
    }
    if reviewed_sha is not None:
        review["reviewed_sha"] = reviewed_sha
    result = sentinel_review(review, status=status)
    result["health"] = {"recommendation": recommendation}
    return result


def large_review_pending_result(reviewed_sha: str) -> dict[str, object]:
    """A measured Large-tier ``review_pending_approval`` sentinel."""
    result = clean_sentinel(reviewed_sha, status="review_pending_approval")
    result["scope"] = {"tier": "large", "files": 17, "lines_actual": 1280}
    return result


def converged_worktree_ledger(worktree: Path, sha: str, ticket_id: str) -> Path:
    """Write a ledger converged at *sha* for *ticket_id* into *worktree*."""
    return write_ledger(worktree, confirmed_ledger(sha=sha, ticket_id=ticket_id))


def clean_client_wt(
    tmp_path: Path, ticket_id: str, *, convergence: str = "clean"
) -> tuple[ClientConfig, Path]:
    """A client with a ``worktree_base`` and its created, git-less worktree dir."""
    base = make_review_client(tmp_path / "ws", convergence=convergence)
    client = base.model_copy(update={"worktree_base": tmp_path / "wts"})
    worktree = worktree_path_for(client, resolve_feature_branch(ticket_id, client))
    worktree.mkdir(parents=True, exist_ok=True)
    return client, worktree


def stamp_review_record(task: TicketTask, sha: str = SEEDED_REVIEW_SHA) -> TicketTask:
    """Seed the v48 approval trio and an attestation at *sha* onto *task*."""
    task.review_approved_at = datetime(2026, 10, 25, tzinfo=UTC)
    task.review_approver_kind = "operator"
    task.review_approved_sha = sha
    task.review_attestation = attestation_for(
        sha, ticket_id=task.ticket_id, client=task.client
    )
    return task


def assert_no_review_record(task: TicketTask) -> None:
    """The approval trio and the attestation are all cleared."""
    assert task.review_approved_at is None
    assert task.review_approver_kind is None
    assert task.review_approved_sha is None
    assert task.review_attestation is None
