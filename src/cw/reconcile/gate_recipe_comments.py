"""Post-lock audit comments for the gate recipes (#1232).

``cw.reconcile.gate_recipes``' act phases each end by posting a best-effort
``gh issue comment`` audit comment for every ticket they released. Those acts
run inside ``reconcile()``'s ``sessions_lock`` hold, so the comment is not
posted there: :func:`defer_gate_recipe_comment_jobs` queues one
:class:`~cw.reconcile.deferred.PostLockJob` per comment on the tick's post-lock
sink, and ``reconcile()`` runs it after the lock releases.

Split out of ``gate_recipes.py`` only to keep that module under the ~1000-line
ceiling; it holds just the comment-deferral helpers.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Protocol

from cw.reconcile.deferred import PostLockJob
from cw.reconcile.tasks import _client_cwd, _is_dangling_client

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.reconcile.deferred import DeferredReconcileJobs

_log = logging.getLogger(__name__)


class _CommentPostFn(Protocol):
    """Shape shared by ``_post_auto_approve_comment``/``_post_auto_adopt_comment``.

    A plain ``Callable[[str, dict[str, object]], None]`` can't express the
    keyword-only ``cwd`` parameter both functions share, so mypy --strict
    wouldn't catch a signature drift at the ``defer_gate_recipe_comment_jobs``
    call site (GitHub #1570).
    """

    def __call__(
        self,
        ticket_id: str,
        snapshot: dict[str, object],
        *,
        cwd: Path | None = None,
    ) -> None: ...


def log_gate_recipe_comment_skipped(ticket_id: str, client: str) -> None:
    """Log a dangling-client audit-comment skip (GitHub #1269/#1279 R7).

    Shared by both gate-recipe act phases (through
    :func:`defer_gate_recipe_comment_jobs`) so the two identical skip sites
    can't drift independently.
    """
    _log.warning(
        "gate_recipe_comment_skipped ticket=%s client=%s: client "
        "missing from clients.yaml (config drift) -- gh call skipped, "
        "GitHub #1269",
        ticket_id,
        client,
    )


def defer_gate_recipe_comment_jobs(
    comment_jobs: list[tuple[str, str, dict[str, object]]],
    clients: dict[str, ClientConfig] | None,
    post_fn: _CommentPostFn,
    *,
    recipe: str,
    deferred: DeferredReconcileJobs,
) -> None:
    """Queue each ``(ticket_id, client, snapshot)`` audit comment for after the lock.

    The dangling-client guard runs here, at defer time, against the act
    phase's own *clients* snapshot (see :func:`log_gate_recipe_comment_skipped`,
    #1269/#1279 R7): a skipped comment is logged and never queued, so no
    unscoped gh call can run later. Every other comment becomes one job,
    labelled ``gate_comment:<recipe>:<client>:<ticket_id>`` so an auto-approve
    and an auto-adopt comment for the same ticket in one tick stay distinct,
    that calls *post_fn* with the client's repo as ``cwd``.
    """
    for ticket_id, client, snapshot in comment_jobs:
        if _is_dangling_client(client, clients or {}):
            log_gate_recipe_comment_skipped(ticket_id, client)
            continue
        deferred.post_lock.append(
            PostLockJob(
                label=f"gate_comment:{recipe}:{client}:{ticket_id}",
                run=partial(
                    post_fn, ticket_id, snapshot, cwd=_client_cwd(client, clients or {})
                ),
            )
        )
