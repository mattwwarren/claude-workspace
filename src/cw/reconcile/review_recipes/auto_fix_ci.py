"""The auto_fix_ci recipe: detect + act for ci_failing PRs (RFC 0010 P4).

Package split (#1315, part 2 of 2). This module owns the ``auto_fix_ci`` recipe —
it reacts to a PR whose ``pr_state`` carries ``attention_state == "ci_failing"``
by re-enqueuing the ticket and running one dispatch tick (a coarse re-entry into
auto-dev, RFC 0010 OQ2), rather than spawning a scoped ``/address-review``.

``_detect_auto_fix_ci`` produces a :class:`ReviewRecipeCandidate` per ci_failing
row (write-free). ``_act_auto_fix_ci`` re-validates each candidate under
``dev_queue_lock()``, emits ``PR_ACTION_TAKEN`` (durably, BEFORE the dispatch),
stamps the one-shot ``auto_fix_ci_fired_at`` latch (GitHub #1206), and — strictly
after the lock releases — re-enqueues + dispatches. A dispatch ``CwError`` or a
cross-repo precondition anomaly (GitHub #1198) emits ``PR_ACTION_FAILED`` instead.

Shared cross-recipe infrastructure (the pure ``_detect_by_attention_state``
classifier, the act-phase helpers, and the recipe/attention/payload constants)
lives in ``cw.reconcile.review_recipes._shared``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.exceptions import CwError
from cw.pr_hydrate import _parse_pr_url, _repo_slug_mismatch
from cw.reconcile.review_recipes._shared import (
    _ATTENTION_CI_FAILING,
    RECIPE_AUTO_FIX_CI,
    ReviewRecipeCandidate,
    _clear_ended_episodes,
    _detect_by_attention_state,
    _emit_pr_action_failed,
    _find_review_task,
    _guard_cross_repo_mismatch,
    _record_pr_action_taken,
    _review_payload_base,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig, TicketTask


_log = logging.getLogger("cw.reconcile.review_recipes")

# RFC 0010 P4 (#1099) — auto_fix_ci payload key. Consumed by exactly one recipe's
# act phase (resident in this module), so it lives here rather than in _shared.py.
_PAYLOAD_KEY_FAILING_CHECKS = "failing_checks"


def _detect_auto_fix_ci(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Detect ci_failing PRs for the auto_fix_ci recipe (RFC 0010 P4)."""
    return _detect_by_attention_state(
        tasks,
        clients=clients,
        config=config,
        attention_state=_ATTENTION_CI_FAILING,
        recipe=RECIPE_AUTO_FIX_CI,
    )


# --- auto_fix_ci act phase (RFC 0010 P4, #1099) ----------------------------


class _RedispatchJob(NamedTuple):
    """Deferred auto_fix_ci re-dispatch built under the lock, run after release.

    Unlike ``_DispatchJob`` this carries no worktree/PR number — the auto_fix_ci
    recipe re-enqueues the ticket and runs a dispatch tick (coarse re-entry into
    auto-dev, RFC 0010 OQ2), it does not spawn a scoped ``/address-review``.
    """

    client: str
    ticket_id: str
    lane: str
    payload_base: dict[str, object]


def _detect_auto_fix_ci_repo_mismatch(
    task: TicketTask, clients: dict[str, ClientConfig]
) -> tuple[str, str] | None:
    """Return ``(pr_repo, client_repo)`` when the row's client workspace resolves
    to a different github repo than its ``pr_url``, else None (GitHub #1198).

    Fails open: an unparseable ``pr_url``, an unresolvable client, or an
    unresolvable workspace remote all yield None (proceed, no anomaly), so the
    guard introduces no new hard-failure branches beyond the mismatch itself.
    """
    parsed = _parse_pr_url(task.pr_url) if task.pr_url is not None else None
    if parsed is None:
        return None
    client_cfg = clients.get(task.client)
    if client_cfg is None:
        return None
    client_repo = _repo_slug_mismatch(parsed[0], client_cfg.workspace_path)
    if client_repo is None:
        return None
    return parsed[0], client_repo


def _prepare_auto_fix_ci_job(
    task: TicketTask,
    session_id: str | None,
    clients: dict[str, ClientConfig],
    now: datetime,
    *,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> _RedispatchJob | None:
    """Re-validate a re-loaded row under the lock; emit + build its re-dispatch.

    Silent skip (no event) when the row is stale — ``pr_state`` gone or no
    longer ``ci_failing`` (a concurrent re-run can have moved it on between
    detect and act) — OR already fired this episode (``auto_fix_ci_fired_at``
    is not None; not an anomaly, mirrors ``_prepare_request_reviewer_job``'s
    already-fired check). An anomaly skip (emits ``PR_ACTION_FAILED``) when the
    row's client resolves to a different repo than its ``pr_url`` (GitHub
    #1198) — both this check and the already-fired check above must pass for
    the row to fire. Otherwise records ``PR_ACTION_TAKEN``
    (emit-before-dispatch), stamps the ``auto_fix_ci_fired_at`` latch to *now*,
    and returns the deferred re-dispatch job.
    """
    pr_state = task.pr_state
    if (
        pr_state is None
        or pr_state.attention_state != _ATTENTION_CI_FAILING
        or task.auto_fix_ci_fired_at is not None
    ):
        return None
    payload_base = _review_payload_base(
        task,
        session_id,
        RECIPE_AUTO_FIX_CI,
        {_PAYLOAD_KEY_FAILING_CHECKS: pr_state.failing_checks},
    )
    # GitHub #1198 — cross-repo dispatch guard. The client's workspace origin
    # remote can resolve to a different repo than the PR's, so re-dispatching
    # auto-dev here would run in the wrong repo. local-only read, no network —
    # safe under dev_queue_lock; do not add network calls here.
    mismatch = _detect_auto_fix_ci_repo_mismatch(task, clients)
    if mismatch is not None:
        pr_repo, client_repo = mismatch
        if not _guard_cross_repo_mismatch(
            task,
            payload_base,
            pr_repo=pr_repo,
            client_repo=client_repo,
            location="client workspace origin",
        ):
            return None
    _record_pr_action_taken(
        payload_base,
        task.client,
        task.ticket_id,
        RECIPE_AUTO_FIX_CI,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
        lane=task.lane,
    )
    task.auto_fix_ci_fired_at = now
    return _RedispatchJob(
        client=task.client,
        ticket_id=task.ticket_id,
        lane=task.lane,
        payload_base=payload_base,
    )


def _dispatch_auto_fix_ci(job: _RedispatchJob) -> str | None:
    """Re-enqueue the ticket then run one dispatch tick (post-lock); id or None.

    Runs strictly AFTER ``dev_queue_lock()`` releases: ``add_ticket``'s own
    internal lock IS ``dev_queue_lock`` (aliased in cw.dev_queue), so calling it
    under our lock would self-deadlock. The function-local imports break the
    ``review_recipes`` -> ``dev_queue``/``dispatch`` import cycle. ``add_ticket``
    raising (``LaneNotFoundError``, a ``CwError``) after ``PR_ACTION_TAKEN`` was
    already emitted -> catch, emit ``PR_ACTION_FAILED`` correction, return None.
    The ``auto_fix_ci_fired_at`` latch stays stamped even when this dispatch
    fails — same posture as ``request_reviewer``: ``PR_ACTION_FAILED`` is the
    visible signal, the latch is NOT rolled back on dispatch failure.

    Why NOT ``force=True`` here (#1362): this call can run either (a) nested
    inside a live loop's own tick (``dispatch_tick`` -> ``_reconcile_usage_limited``
    -> ``reconcile`` -> this recipe, same process already holding
    ``dispatch_loop_lock()``) or (b) standalone from ``cw status``/``cw
    start``/``cw doctor``, which call ``reconcile()`` directly with no lock
    held at all. ``force=True`` cannot distinguish these -- it would
    unconditionally bypass the lock in case (b) too, silently permitting a
    genuinely concurrent second dispatch tick against whatever OTHER process
    actually holds the lock elsewhere, reintroducing the exact per-process
    state divergence #1362 exists to prevent. Left unforced, a
    ``DispatchLoopLockedError`` here is caught by ``except CwError`` below —
    the SAME accepted-degradation posture already used for
    ``SessionsLockReentryError`` on this identical call site (GitHub #1228):
    the ticket is already durably re-enqueued (``add_ticket`` above already
    ran), so a lock-contention failure here only costs the "trigger a tick
    right now" nicety, not the fix itself -- whichever loop actually holds
    the lock will pick the re-enqueued ticket up on its own next regular tick
    (default 30s, ``tick_interval_seconds`` in ``config.py``).
    """
    from cw.dev_queue import add_ticket
    from cw.dispatch import run_dispatch_loop
    from cw.models import TicketTask

    try:
        add_ticket(
            TicketTask(
                ticket_id=job.ticket_id,
                client=job.client,
                lane=job.lane,
                # #1631: this constructs a brand-new row for a ticket that has
                # not yet spawned a session under it -- the same positive-proof
                # shape dev_queue_add's construction site has. The model
                # default (True, fail-open) is for rows whose history is
                # unknown; this row's history IS known, so it must not inherit
                # the default.
                ever_spawned=False,
            )
        )
        run_dispatch_loop(once=True, client=job.client, emit=None)
    except CwError as exc:
        _log.warning(
            "review_recipe_redispatch_failed ticket=%s",
            job.ticket_id,
            exc_info=True,
        )
        _emit_pr_action_failed(
            job.payload_base, error=str(exc), ticket_id=job.ticket_id
        )
        return None
    return job.ticket_id


def _clear_auto_fix_ci_fired(task: TicketTask) -> None:
    task.auto_fix_ci_fired_at = None


def _act_auto_fix_ci(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    now: datetime | None = None,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> list[str]:
    """Act phase for auto_fix_ci: re-validate under lock, emit, then re-dispatch.

    Mirrors ``_act_request_reviewer``'s shape. Under one ``dev_queue_lock()``:

    1. **Episode-end clear** — scan every freshly-loaded row and clear
       ``auto_fix_ci_fired_at`` where it is set but the current ``pr_state`` is
       None or no longer ``ci_failing`` (the episode ended; this re-arms the
       latch for a genuine future re-entry). Runs regardless of whether
       *candidates* is empty.
    2. **Fire** — for each candidate, ``_prepare_auto_fix_ci_job`` re-validates
       both the latch and the cross-repo dispatch guard (GitHub #1198;
       ``clients`` is threaded through for the guard), emits
       ``PR_ACTION_TAKEN``, and stamps the latch.

    Stamping/clearing the latch IS a dev-queue write (GitHub #1206: all four
    review-recipe act phases now perform this same kind of write — a latch
    field, not a status transition; none remain read-only), saved before the
    lock releases. The re-enqueue +
    dispatch tick runs strictly after the lock releases (``add_ticket``
    re-acquires ``dev_queue_lock``, so nesting would self-deadlock). Returns
    the ticket_ids whose re-dispatch succeeded.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    jobs: list[_RedispatchJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = _clear_ended_episodes(
            store,
            attention_state=_ATTENTION_CI_FAILING,
            get_fired_at=lambda t: t.auto_fix_ci_fired_at,
            clear_fired_at=_clear_auto_fix_ci_fired,
        )
        for candidate in by_key.values():
            task = _find_review_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            job = _prepare_auto_fix_ci_job(
                task,
                candidate.session_id,
                clients,
                resolved_now,
                config=config,
                repeat_fire_counts=repeat_fire_counts,
            )
            if job is not None:
                jobs.append(job)
                changed = True
        if changed:
            save_dev_queue(store)
    acted: list[str] = []
    for job in jobs:
        ticket_id = _dispatch_auto_fix_ci(job)
        if ticket_id is not None:
            acted.append(ticket_id)
    return acted
