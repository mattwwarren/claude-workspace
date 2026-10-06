"""Tests for cw.reconcile.gate_recipe_comments (#1232).

The gate recipes' audit comments are ``gh issue comment`` calls. They are no
longer posted from inside ``reconcile()``'s ``sessions_lock`` hold: the act
phases queue one :class:`~cw.reconcile.deferred.PostLockJob` per comment on the
tick's post-lock sink, and ``reconcile()`` runs them after the lock releases.
These tests drive ``defer_gate_recipe_comment_jobs`` with a fake ``post_fn``,
so no gh stub is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cw.reconcile.gate_recipe_comments import defer_gate_recipe_comment_jobs

from cw.models import ClientConfig
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.gate_recipes import RECIPE_AUTO_ADOPT_PLAN, RECIPE_AUTO_APPROVE_REVIEW

_SNAPSHOT: dict[str, object] = {"recommendation": "PROCEED"}


class _PostRecorder:
    """A fake ``_CommentPostFn`` recording each post it is asked to make."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], Path | None]] = []

    def __call__(
        self,
        ticket_id: str,
        snapshot: dict[str, object],
        *,
        cwd: Path | None = None,
    ) -> None:
        self.calls.append((ticket_id, snapshot, cwd))


def _clients(tmp_path: Path, *names: str) -> dict[str, ClientConfig]:
    clients: dict[str, ClientConfig] = {}
    for name in names:
        workspace = tmp_path / name
        workspace.mkdir()
        clients[name] = ClientConfig(
            name=name, workspace_path=workspace, default_branch="main"
        )
    return clients


def test_queues_one_labelled_job_per_comment_and_posts_only_on_drain(
    tmp_path: Path,
) -> None:
    clients = _clients(tmp_path, "acme", "beta")
    post = _PostRecorder()
    deferred = DeferredReconcileJobs()

    defer_gate_recipe_comment_jobs(
        [("GEN-1", "acme", _SNAPSHOT), ("GEN-2", "beta", _SNAPSHOT)],
        clients,
        post,
        recipe=RECIPE_AUTO_APPROVE_REVIEW,
        deferred=deferred,
    )

    assert [job.label for job in deferred.post_lock] == [
        f"gate_comment:{RECIPE_AUTO_APPROVE_REVIEW}:acme:GEN-1",
        f"gate_comment:{RECIPE_AUTO_APPROVE_REVIEW}:beta:GEN-2",
    ]
    assert post.calls == []

    run_post_lock_jobs(deferred)

    # Each post is scoped to its own client's repo (_client_cwd, #1279).
    assert post.calls == [
        ("GEN-1", _SNAPSHOT, tmp_path / "acme"),
        ("GEN-2", _SNAPSHOT, tmp_path / "beta"),
    ]


def test_an_approve_and_an_adopt_comment_for_one_ticket_both_queue(
    tmp_path: Path,
) -> None:
    """The label carries the recipe, so two comments for the same ticket in
    one tick are two jobs, and both post."""
    clients = _clients(tmp_path, "acme")
    approve_post = _PostRecorder()
    adopt_post = _PostRecorder()
    deferred = DeferredReconcileJobs()

    defer_gate_recipe_comment_jobs(
        [("GEN-1", "acme", _SNAPSHOT)],
        clients,
        approve_post,
        recipe=RECIPE_AUTO_APPROVE_REVIEW,
        deferred=deferred,
    )
    defer_gate_recipe_comment_jobs(
        [("GEN-1", "acme", _SNAPSHOT)],
        clients,
        adopt_post,
        recipe=RECIPE_AUTO_ADOPT_PLAN,
        deferred=deferred,
    )
    run_post_lock_jobs(deferred)

    assert [job.label for job in deferred.post_lock] == [
        f"gate_comment:{RECIPE_AUTO_APPROVE_REVIEW}:acme:GEN-1",
        f"gate_comment:{RECIPE_AUTO_ADOPT_PLAN}:acme:GEN-1",
    ]
    assert [call[0] for call in approve_post.calls] == ["GEN-1"]
    assert [call[0] for call in adopt_post.calls] == ["GEN-1"]


def test_a_dangling_client_is_skipped_at_defer_time(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A client absent from a populated clients dict (config drift, #1269) is
    logged and never queued, so no unscoped gh call can run later."""
    clients = _clients(tmp_path, "beta")
    post = _PostRecorder()
    deferred = DeferredReconcileJobs()

    with caplog.at_level("WARNING"):
        defer_gate_recipe_comment_jobs(
            [("GEN-1", "acme", _SNAPSHOT), ("GEN-2", "beta", _SNAPSHOT)],
            clients,
            post,
            recipe=RECIPE_AUTO_APPROVE_REVIEW,
            deferred=deferred,
        )

    assert [job.label for job in deferred.post_lock] == [
        f"gate_comment:{RECIPE_AUTO_APPROVE_REVIEW}:beta:GEN-2"
    ]
    assert "gate_recipe_comment_skipped ticket=GEN-1 client=acme" in caplog.text
    run_post_lock_jobs(deferred)
    assert [call[0] for call in post.calls] == ["GEN-2"]


def test_no_clients_config_queues_an_ambient_cwd_post() -> None:
    """With no clients configured at all (single-tenant, ``clients=None``)
    nothing is dangling, and the post keeps the ambient cwd (``None``)."""
    post = _PostRecorder()
    deferred = DeferredReconcileJobs()

    defer_gate_recipe_comment_jobs(
        [("GEN-1", "acme", _SNAPSHOT)],
        None,
        post,
        recipe=RECIPE_AUTO_ADOPT_PLAN,
        deferred=deferred,
    )
    run_post_lock_jobs(deferred)

    assert post.calls == [("GEN-1", _SNAPSHOT, None)]
