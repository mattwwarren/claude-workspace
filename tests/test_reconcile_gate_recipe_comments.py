"""Tests for cw.reconcile.gate_recipe_comments (#1232).

The gate recipes' audit comments are outbox enqueues (RFC 0019 O3b). They are
no longer written from inside ``reconcile()``'s ``sessions_lock`` hold: the act
phases queue one :class:`~cw.reconcile.deferred.PostLockJob` per comment on the
tick's post-lock sink, and ``reconcile()`` runs them after the lock releases.
The deferral tests drive ``defer_gate_recipe_comment_jobs`` with a fake
``post_fn``; the post-function tests read the enqueued outbox entry back.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cw.models import ClientConfig
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.gate_recipe_comments import (
    AUTO_ADOPT_COMMENT_TEMPLATE,
    AUTO_APPROVE_COMMENT_TEMPLATE,
    defer_gate_recipe_comment_jobs,
    post_auto_adopt_comment,
    post_auto_approve_comment,
)
from cw.reconcile.gate_recipes import RECIPE_AUTO_ADOPT_PLAN, RECIPE_AUTO_APPROVE_REVIEW
from tests._shared.outbox_builders import entries_for

_SNAPSHOT: dict[str, object] = {"recommendation": "PROCEED"}


class _PostRecorder:
    """A fake ``_CommentPostFn`` recording each post it is asked to make."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], str, str, str]] = []

    def __call__(
        self,
        ticket_id: str,
        snapshot: dict[str, object],
        *,
        client: str,
        session_id: str,
        recipe: str,
    ) -> None:
        self.calls.append((ticket_id, snapshot, client, session_id, recipe))


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
        [
            ("GEN-1", "acme", _SNAPSHOT, "sess-a"),
            ("GEN-2", "beta", _SNAPSHOT, "sess-b"),
        ],
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

    # Each post carries its own client, its own session and the deferral's recipe.
    assert post.calls == [
        ("GEN-1", _SNAPSHOT, "acme", "sess-a", RECIPE_AUTO_APPROVE_REVIEW),
        ("GEN-2", _SNAPSHOT, "beta", "sess-b", RECIPE_AUTO_APPROVE_REVIEW),
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
        [("GEN-1", "acme", _SNAPSHOT, "sess-1")],
        clients,
        approve_post,
        recipe=RECIPE_AUTO_APPROVE_REVIEW,
        deferred=deferred,
    )
    defer_gate_recipe_comment_jobs(
        [("GEN-1", "acme", _SNAPSHOT, "sess-1")],
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
    assert [(call[0], call[4]) for call in approve_post.calls] == [
        ("GEN-1", RECIPE_AUTO_APPROVE_REVIEW)
    ]
    assert [(call[0], call[4]) for call in adopt_post.calls] == [
        ("GEN-1", RECIPE_AUTO_ADOPT_PLAN)
    ]


def test_a_dangling_client_is_skipped_at_defer_time(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A client absent from a populated clients dict (config drift, #1269) is
    logged and never queued, so nothing is enqueued for a client cw can't resolve."""
    clients = _clients(tmp_path, "beta")
    post = _PostRecorder()
    deferred = DeferredReconcileJobs()

    with caplog.at_level("WARNING"):
        defer_gate_recipe_comment_jobs(
            [
                ("GEN-1", "acme", _SNAPSHOT, "sess-1"),
                ("GEN-2", "beta", _SNAPSHOT, "sess-2"),
            ],
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


def test_no_clients_config_queues_the_post_unscoped() -> None:
    """With no clients configured at all (single-tenant, ``clients=None``)
    nothing is dangling, so the job is queued and the post gets its client."""
    post = _PostRecorder()
    deferred = DeferredReconcileJobs()

    defer_gate_recipe_comment_jobs(
        [("GEN-1", "acme", _SNAPSHOT, "sess-1")],
        None,
        post,
        recipe=RECIPE_AUTO_ADOPT_PLAN,
        deferred=deferred,
    )
    run_post_lock_jobs(deferred)

    assert post.calls == [
        ("GEN-1", _SNAPSHOT, "acme", "sess-1", RECIPE_AUTO_ADOPT_PLAN)
    ]


_APPROVE_SNAPSHOT: dict[str, object] = {
    "must_fix_initial": 2,
    "deferred": 1,
    "recommendation": "PROCEED",
    "forbidden_touched": False,
    "agents_run": 3,
}

_ADOPT_SNAPSHOT: dict[str, object] = {
    "tier": "large",
    "files": 12,
    "lines_estimate": 640,
    "forbidden_touched": False,
    "plan_draft_fingerprint": "f" * 64,
    "plan_reviewed": True,
}


def _digest8(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


class TestPostAutoApproveComment:
    def test_enqueues_the_formatted_template_under_the_recipe_and_session_marker(
        self,
    ) -> None:
        post_auto_approve_comment(
            "GEN-1",
            _APPROVE_SNAPSHOT,
            client="acme",
            session_id="sess-1",
            recipe=RECIPE_AUTO_APPROVE_REVIEW,
        )

        expected = AUTO_APPROVE_COMMENT_TEMPLATE.format(
            recipe=RECIPE_AUTO_APPROVE_REVIEW,
            must_fix_initial=2,
            deferred=1,
            recommendation="PROCEED",
            forbidden_touched=False,
            agents_run=3,
        )
        ((entry, parts),) = entries_for("acme")
        assert parts == [expected]
        assert entry.ticket == "GEN-1"
        assert entry.producer == "gate-recipe"
        assert entry.marker == (
            f"gate-auto-approve-clean-review-sess-1-{_digest8(expected)}"
        )
        assert entry.operator_authored is False
        assert entry.redacted is False
        assert "- must_fix_initial: 2" in parts[0]
        assert "- agents_run: 3" in parts[0]

    def test_same_session_and_body_dedups(self) -> None:
        for _ in range(2):
            post_auto_approve_comment(
                "GEN-1",
                _APPROVE_SNAPSHOT,
                client="acme",
                session_id="sess-1",
                recipe=RECIPE_AUTO_APPROVE_REVIEW,
            )

        assert len(entries_for("acme")) == 1

    def test_a_different_body_in_the_same_session_is_a_second_entry(self) -> None:
        post_auto_approve_comment(
            "GEN-1",
            _APPROVE_SNAPSHOT,
            client="acme",
            session_id="sess-1",
            recipe=RECIPE_AUTO_APPROVE_REVIEW,
        )
        post_auto_approve_comment(
            "GEN-1",
            {**_APPROVE_SNAPSHOT, "agents_run": 4},
            client="acme",
            session_id="sess-1",
            recipe=RECIPE_AUTO_APPROVE_REVIEW,
        )

        first, second = entries_for("acme")
        assert first.entry.marker != second.entry.marker

    def test_an_overlong_session_id_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The marker is capped at 128 characters; a session id that pushes it
        past that is a skipped reason and a log line, never an exception."""
        with caplog.at_level("WARNING"):
            post_auto_approve_comment(
                "GEN-1",
                _APPROVE_SNAPSHOT,
                client="acme",
                session_id="s" * 120,
                recipe=RECIPE_AUTO_APPROVE_REVIEW,
            )

        assert entries_for("acme") == []
        assert (
            "gate_recipe_comment_enqueue_failed ticket=GEN-1 client=acme "
            "reason=outbox_enqueue_failed"
        ) in caplog.text


class TestPostAutoAdoptComment:
    def test_enqueues_the_formatted_template_under_the_recipe_and_session_marker(
        self,
    ) -> None:
        post_auto_adopt_comment(
            "GEN-2",
            _ADOPT_SNAPSHOT,
            client="beta",
            session_id="sess-9",
            recipe=RECIPE_AUTO_ADOPT_PLAN,
        )

        expected = AUTO_ADOPT_COMMENT_TEMPLATE.format(
            recipe=RECIPE_AUTO_ADOPT_PLAN,
            tier="large",
            files=12,
            lines_estimate=640,
            forbidden_touched=False,
            plan_draft_fingerprint="f" * 64,
            plan_reviewed=True,
        )
        assert entries_for("acme") == []
        ((entry, parts),) = entries_for("beta")
        assert parts == [expected]
        assert entry.ticket == "GEN-2"
        assert entry.producer == "gate-recipe"
        assert entry.marker == f"gate-auto-adopt-clean-plan-sess-9-{_digest8(expected)}"
        assert entry.operator_authored is False
        assert entry.redacted is False
        assert "- scope: large (12 files, ~640 lines)" in parts[0]
        assert f"- plan_draft_fingerprint: {'f' * 64}" in parts[0]

    def test_a_missing_snapshot_key_renders_none_rather_than_raising(self) -> None:
        """The adopt template reads the snapshot with ``.get``: an absent key
        is the literal ``None`` in the comment, not a ``KeyError`` that would
        escape the post-lock job."""
        post_auto_adopt_comment(
            "GEN-2",
            {},
            client="acme",
            session_id="sess-9",
            recipe=RECIPE_AUTO_ADOPT_PLAN,
        )

        ((_, parts),) = entries_for("acme")
        assert "- scope: None (None files, ~None lines)" in parts[0]

    def test_an_overlong_session_id_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            post_auto_adopt_comment(
                "GEN-2",
                _ADOPT_SNAPSHOT,
                client="acme",
                session_id="s" * 120,
                recipe=RECIPE_AUTO_ADOPT_PLAN,
            )

        assert entries_for("acme") == []
        assert "gate_recipe_comment_enqueue_failed ticket=GEN-2" in caplog.text
