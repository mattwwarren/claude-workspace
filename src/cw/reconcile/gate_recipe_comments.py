"""Post-lock audit comments for the gate recipes (#1232).

``cw.reconcile.gate_recipes``' act phases each end by queuing an audit comment
for every ticket they released. Those acts run inside ``reconcile()``'s
``sessions_lock`` hold, so the comment is not queued there:
:func:`defer_gate_recipe_comment_jobs` queues one
:class:`~cw.reconcile.deferred.PostLockJob` per comment on the tick's post-lock
sink, and ``reconcile()`` runs it after the lock releases.

Each job enqueues the comment on the outbox (RFC 0019 O3b): the drain posts it
for any tracker, so a Linear client's gate comment is queued rather than
failing on a ``gh`` call. The enqueue is best-effort: a failure is logged and
never undoes the approval the event trail already records.

Split out of ``gate_recipes.py`` only to keep that module under the ~1000-line
ceiling; it holds the comment-deferral helpers, the two audit-comment
templates (#2545 moved the templates here for the same reason) and the two
post functions.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Protocol

from cw.outbox_producer import (
    SKIP_ALREADY_POSTED,
    SKIP_ENQUEUE_FAILED,
    enqueue_prose,
    try_content_marker,
)
from cw.reconcile.deferred import PostLockJob
from cw.reconcile.gate_predicates import (
    _SNAPSHOT_KEY_FILES,
    _SNAPSHOT_KEY_FINGERPRINT,
    _SNAPSHOT_KEY_FORBIDDEN,
    _SNAPSHOT_KEY_LINES,
    _SNAPSHOT_KEY_REVIEWED,
    _SNAPSHOT_KEY_TIER,
)
from cw.reconcile.tasks import _is_dangling_client

if TYPE_CHECKING:
    from cw.models import ClientConfig
    from cw.reconcile.deferred import DeferredReconcileJobs

_log = logging.getLogger(__name__)

# Digest characters kept in a gate comment's outbox marker. Eight is enough:
# the marker already carries the recipe and the session id, so the digest only
# separates two comments of one session that differ in body.
_GATE_MARKER_DIGEST_CHARS = 8

# Audit-comment body for a review gate the ``auto_approve_clean_review``
# recipe released (formatted by :func:`post_auto_approve_comment`).
AUTO_APPROVE_COMMENT_TEMPLATE = """\
Auto-approved by gate recipe `{recipe}`.

The review met the clean-review predicate and was approved automatically
(no human review) by RFC 0009 gate-recipe automation:

- must_fix_initial: {must_fix_initial}
- deferred: {deferred}
- recommendation: {recommendation}
- forbidden_touched: {forbidden_touched}
- agents_run: {agents_run}

See event `GATE_AUTO_APPROVED` for the full audit trail.
"""

# Audit-comment body for a plan gate the ``auto_adopt_clean_plan`` recipe
# released (formatted by :func:`post_auto_adopt_comment`).
AUTO_ADOPT_COMMENT_TEMPLATE = """\
Auto-approved by gate recipe `{recipe}`.

The plan met the clean-plan predicate (no forbidden-area touch, no operator
`scope_hint: large`, and a draft fingerprint the approval is bound to) and was
approved automatically (no human review) by RFC 0009 gate-recipe automation:

- scope: {tier} ({files} files, ~{lines_estimate} lines)
- forbidden_touched: {forbidden_touched}
- plan_draft_fingerprint: {plan_draft_fingerprint}
- plan_reviewed: {plan_reviewed}

An unreviewed plan returns to the plan stage, which runs Plan Quality Review
before implementation starts. See event
`GATE_AUTO_APPROVED` for the full audit trail.
"""


class _CommentPostFn(Protocol):
    """Shape shared by the two gate-comment post functions below.

    The keyword-only ``client``, ``session_id`` and ``recipe`` are bound by
    :func:`defer_gate_recipe_comment_jobs`, so a signature drift between the
    two post functions and the deferral call site fails ``mypy --strict``
    (GitHub #1570).
    """

    def __call__(
        self,
        ticket_id: str,
        snapshot: dict[str, object],
        *,
        client: str,
        session_id: str,
        recipe: str,
    ) -> None: ...


def log_gate_recipe_comment_skipped(ticket_id: str, client: str) -> None:
    """Log a dangling-client audit-comment skip (GitHub #1269/#1279 R7).

    Shared by both gate-recipe act phases (through
    :func:`defer_gate_recipe_comment_jobs`) so the two identical skip sites
    can't drift independently.
    """
    _log.warning(
        "gate_recipe_comment_skipped ticket=%s client=%s: client "
        "missing from clients.yaml (config drift) -- comment not queued, "
        "GitHub #1269",
        ticket_id,
        client,
    )


def defer_gate_recipe_comment_jobs(
    comment_jobs: list[tuple[str, str, dict[str, object], str]],
    clients: dict[str, ClientConfig] | None,
    post_fn: _CommentPostFn,
    *,
    recipe: str,
    deferred: DeferredReconcileJobs,
) -> None:
    """Queue each ``(ticket_id, client, snapshot, session_id)`` job for after the lock.

    The dangling-client guard runs here, at defer time, against the act
    phase's own *clients* snapshot (see :func:`log_gate_recipe_comment_skipped`,
    #1269/#1279 R7): a skipped comment is logged and never queued. Every other
    comment becomes one job, labelled ``gate_comment:<recipe>:<client>:<ticket_id>``
    so an auto-approve and an auto-adopt comment for the same ticket in one
    tick stay distinct, that calls *post_fn* with *client*, *session_id* and
    *recipe* bound.
    """
    for ticket_id, client, snapshot, session_id in comment_jobs:
        if _is_dangling_client(client, clients or {}):
            log_gate_recipe_comment_skipped(ticket_id, client)
            continue
        deferred.post_lock.append(
            PostLockJob(
                label=f"gate_comment:{recipe}:{client}:{ticket_id}",
                run=partial(
                    post_fn,
                    ticket_id,
                    snapshot,
                    client=client,
                    session_id=session_id,
                    recipe=recipe,
                ),
            )
        )


def _enqueue_gate_comment(
    ticket_id: str, client: str, recipe: str, session_id: str, body: str
) -> None:
    """Queue *body* as the gate comment for *ticket_id*; a failure is logged.

    The marker names the recipe and the session, so two gate releases of one
    ticket in different sessions are two entries, and a retry of the same
    session and body dedups. Failures never raise: the gate event is the
    audit record, and the approval stands regardless.
    """
    marker = try_content_marker(
        f"gate-{recipe.replace('_', '-')}-{session_id}",
        body,
        chars=_GATE_MARKER_DIGEST_CHARS,
    )
    if marker is None:
        reason = SKIP_ENQUEUE_FAILED
    else:
        outcome = enqueue_prose(client, ticket_id, "gate-recipe", marker, (body,))
        if outcome.created or outcome.skipped_reason == SKIP_ALREADY_POSTED:
            return
        reason = outcome.skipped_reason or SKIP_ENQUEUE_FAILED
    _log.warning(
        "gate_recipe_comment_enqueue_failed ticket=%s client=%s reason=%s",
        ticket_id,
        client,
        reason,
    )


def post_auto_approve_comment(
    ticket_id: str,
    snapshot: dict[str, object],
    *,
    client: str,
    session_id: str,
    recipe: str,
) -> None:
    """Queue the auto-approve audit comment for the ticket (best-effort, logged).

    A distinct helper from ``codex_review_persist``'s review comment: that one
    swallows failures with zero logging, whereas the ticket's OQ2 resolution
    requires a comment-write failure to be logged (the event remains the
    source-of-truth audit trail — a failed comment never undoes the approve).
    """
    body = AUTO_APPROVE_COMMENT_TEMPLATE.format(
        recipe=recipe,
        must_fix_initial=snapshot["must_fix_initial"],
        deferred=snapshot["deferred"],
        recommendation=snapshot["recommendation"],
        forbidden_touched=snapshot["forbidden_touched"],
        agents_run=snapshot["agents_run"],
    )
    _enqueue_gate_comment(ticket_id, client, recipe, session_id, body)


def post_auto_adopt_comment(
    ticket_id: str,
    snapshot: dict[str, object],
    *,
    client: str,
    session_id: str,
    recipe: str,
) -> None:
    """Queue the auto-adopt audit comment for the ticket (best-effort, logged).

    Mirrors :func:`post_auto_approve_comment`, formatting the plan template with
    the predicate snapshot's fields.
    """
    # Explicit named args, not **snapshot: this writes to a public,
    # append-only GitHub comment, so the fields exposed there must stay
    # grep-able and reviewable at this call site. Unpacking the full
    # snapshot dict would auto-expose any future key added to
    # _clean_plan_snapshot with no code change here to acknowledge the new
    # public disclosure, and would raise TypeError if a future key ever
    # collided with the `recipe=` kwarg.
    body = AUTO_ADOPT_COMMENT_TEMPLATE.format(
        recipe=recipe,
        tier=snapshot.get(_SNAPSHOT_KEY_TIER),
        files=snapshot.get(_SNAPSHOT_KEY_FILES),
        lines_estimate=snapshot.get(_SNAPSHOT_KEY_LINES),
        forbidden_touched=snapshot.get(_SNAPSHOT_KEY_FORBIDDEN),
        plan_draft_fingerprint=snapshot.get(_SNAPSHOT_KEY_FINGERPRINT),
        plan_reviewed=snapshot.get(_SNAPSHOT_KEY_REVIEWED),
    )
    _enqueue_gate_comment(ticket_id, client, recipe, session_id, body)
